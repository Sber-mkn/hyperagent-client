import json
import logging
import os
import pathlib
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any

import pika

from client.core.client_state import (
    ACCESS_ASK,
    ACCESS_FULL,
    ACCESS_READ_ONLY,
    DEFAULT_RABBITMQ_PORT,
)
from client.core.rabbitmq_service import RabbitMQBase

USER = "client"
PASSWORD = "12345"
AGENT_EXCHANGE = "agent_exchange"
AGENT_ROUTING_KEY = "agent"
EXCHANGE = "router_exchange"
ROUTER_QUEUE = "router_queue"
CLIENT_QUEUE = "client_queue"
ROUTING_KEY = "router"
SUPERVISOR_ROUTING_KEY = "supervisor"
DEFAULT_WORKDIR = pathlib.Path(os.getenv("CLIENT_WORKDIR", "workdir"))
DEFAULT_COMMAND_TIMEOUT = 60
DEFAULT_OUTPUT_LIMIT = 4000
MAX_OUTPUT_CHARS = 20000
# The GUI build has no console of its own, so Windows hands every console child
# a brand new window — which flashes open and shut on the user's screen for each
# command. Output is captured through pipes either way. Absent off Windows.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

logger = logging.getLogger(__name__)


def _find_bash() -> str:
    """A bare "bash" on Windows usually resolves to the WSL launcher, whose
    filesystem and Python are not the ones the rest of the machine uses, so
    Git Bash is preferred and WSL stubs are skipped when scanning PATH."""
    if platform.system() != "Windows":
        return "bash"

    preferred = (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
    )
    for exe in preferred:
        if os.path.isfile(exe):
            return exe

    skip_markers = ("windowsapps", "system32", "syswow64", "sysnative")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        exe = os.path.join(entry, "bash.exe")
        if os.path.isfile(exe) and not any(marker in exe.lower() for marker in skip_markers):
            return exe

    return "bash"


def _find_powershell() -> str:
    """Absolute paths first, PATH only as a fallback. A PATH that has lost
    System32 — a user variable that overflowed and got truncated, a launcher
    started with a trimmed environment — is precisely the case where a bare
    "powershell" fails to resolve, and Windows PowerShell is always present at
    the fixed location below."""
    if platform.system() != "Windows":
        return shutil.which("pwsh") or "pwsh"

    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    preferred = (
        rf"{program_files}\PowerShell\7\pwsh.exe",
        rf"{system_root}\System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    for exe in preferred:
        if os.path.isfile(exe):
            return exe

    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _strip_ansi(text: str) -> str:
    """Colour codes survive redirection in pwsh 7 and in plenty of CLI tools, so
    captured output arrives full of escape sequences. They mean nothing to the
    agent reading it and only burn context."""
    return _ANSI.sub("", text)


def _truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n...[обрезано {cut} символов]...\n{text[-tail:]}"


def _tool_error(text: str) -> dict[str, Any]:
    return {"result": text, "stdout": "", "stderr": text, "returncode": 1}


def parse_client_command(command: Any) -> tuple[str, dict[str, Any]]:
    """The agent addresses a client-side tool by name, passing the model's raw
    JSON arguments: {"name": "run_bash", "arguments": '{"command": "ls"}'}."""
    if isinstance(command, str):  # older agent builds sent a bare shell string
        return "run_bash", {"command": command}
    if not isinstance(command, dict):
        return "", {}

    arguments = command.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return str(command.get("name") or ""), arguments


def command_text(name: str, arguments: dict[str, Any]) -> str:
    """What the user sees in the command block and the permission dialog."""
    if name in ("run_bash", "run_powershell", "run_in_terminal"):
        return str(arguments.get("command") or "")
    if name == "ask_user":
        return str(arguments.get("question") or "")
    return f"{name}({json.dumps(arguments, ensure_ascii=False)})"


class RabbitMQClient(RabbitMQBase):
    def __init__(self, event_handler, host: str, port: int = DEFAULT_RABBITMQ_PORT):
        super().__init__(
            USER,
            PASSWORD,
            EXCHANGE,
            CLIENT_QUEUE,
            ROUTING_KEY,
            host=host,
            port=port,
        )
        self._router_host = host
        self._router_port = port
        self._rpc_user = USER
        self._rpc_password = PASSWORD
        self._rpc_host = host
        self._rpc_port = port
        self.agent_session = {}
        self.work_dir = DEFAULT_WORKDIR
        self.allow_commands_for_request = False
        self.ready_event = threading.Event()
        self.event_handler = event_handler
        self.login = None

    def set_work_dir(self, work_dir: str | None) -> None:
        self.work_dir = pathlib.Path(work_dir) if work_dir else DEFAULT_WORKDIR

    def _emit(self, event_name: str, *args) -> None:
        handler = getattr(self.event_handler, event_name, None)
        if handler is not None:
            handler(*args)

    def send_login(
        self,
        login: str,
        password: str,
    ) -> bool:
        response = self.request_response(
            {
                "type": "login",
                "login": login,
                "password": password,
            },
            routing_key="router",
            timeout=300,
        )
        if response.get("type") == "login_response":
            self.login = login
            self._reconnect_to_personal(response)
            self.publish_message({"type": "login"}, routing_key=SUPERVISOR_ROUTING_KEY)
            return True

        self._emit(
            "on_login_error",
            response.get("error") or response.get("message") or "Invalid login or password",
        )
        return False

    def send_task(self, task: str, chat_id: int) -> None:
        message = {
            "task": task,
            "command": "start",
            "agent_session": {**self.agent_session, "chat_id": chat_id},
        }
        self.connection.add_callback_threadsafe(lambda: self._publish(message))

    def list_chats(self) -> list[dict]:
        response = self._supervisor_request({"type": "client_data", "action": "list_chats"})
        return response["chats"]

    def create_chat(self, title: str) -> dict:
        response = self._supervisor_request(
            {"type": "client_data", "action": "create_chat", "title": title}
        )
        return response["chat"]

    def rename_chat(self, chat_id: int, title: str) -> dict:
        response = self._supervisor_request(
            {
                "type": "client_data",
                "action": "rename_chat",
                "chat_id": chat_id,
                "title": title,
            }
        )
        return response["chat"]

    def list_models(self, provider: str, url: str = "") -> list[str]:
        """Ask the backend which models it can reach. Model addresses are the
        server's to resolve — this client may not even be on the same network."""
        response = self._supervisor_request(
            {"type": "client_data", "action": "list_models", "provider": provider, "url": url},
            timeout=20.0,
        )
        return list(response["models"])

    def get_chat_history(self, chat_id: int) -> list[dict]:
        response = self._supervisor_request(
            {"type": "client_data", "action": "get_history", "chat_id": chat_id}
        )
        return response["messages"]

    def add_client_message(self, chat_id: int, message_type: str, message) -> int:
        response = self._supervisor_request(
            {
                "type": "client_data",
                "action": "add_client_message",
                "chat_id": chat_id,
                "message_type": message_type,
                "message": message,
            }
        )
        return int(response["id"])

    def receive_message(self, ch, method, properties, body):
        try:
            message = json.loads(body.decode("utf-8"))
            message_type = message.get("type", "")
            logger.info("Received message: %s", message_type)

            if message_type == "ready":
                self.allow_commands_for_request = False
                self.ready_event.set()
                self._emit("on_ready")
                ch.basic_ack(delivery_tag=method.delivery_tag)

            elif message_type == "result":
                self._emit("on_result", message)
                ch.basic_ack(delivery_tag=method.delivery_tag)

            elif message_type == "login_error":
                self._emit(
                    "on_login_error",
                    message.get("error") or message.get("message") or "Неправильный пароль",
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)

            elif message_type == "service_unavailable":
                self._emit(
                    "on_service_unavailable",
                    message.get("error") or message.get("message") or "Сервис временно недоступен",
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)

            elif message_type == "agent_message":
                self._emit(
                    "on_agent_message",
                    message.get("message_type"),
                    message.get("message"),
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)

            elif message_type == "error":
                self._emit(
                    "on_agent_message",
                    "error",
                    message.get("error") or message.get("message") or "Agent error",
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)

            elif message_type == "client_command":
                work_dir = self.work_dir
                work_dir.mkdir(parents=True, exist_ok=True)
                command_id = properties.correlation_id or str(method.delivery_tag)
                name, arguments = parse_client_command(message.get("command"))
                command_info = {
                    "id": command_id,
                    "name": name,
                    "command": command_text(name, arguments),
                    "cwd": str(work_dir),
                }
                self._emit(
                    "on_client_command_start",
                    command_info,
                )
                if name == "ask_user":
                    # Asking a question runs nothing on the machine, so it is
                    # not subject to the command-execution access level.
                    response = self._ask_user(command_info["command"])
                elif self._can_run_command(command_info):
                    response = self._execute_client_tool(name, arguments, work_dir)
                else:
                    response = _tool_error("read only")

                response = {
                    **response,
                    "command": command_info["command"],
                    "cwd": str(work_dir),
                }
                self._emit(
                    "on_client_command_result",
                    {
                        "id": command_id,
                        **response,
                    },
                )

                self.send_response(
                    properties.reply_to,
                    properties.correlation_id,
                    response,
                )

                ch.basic_ack(delivery_tag=method.delivery_tag)

            else:
                logger.warning("Unknown message type: %s", message_type)
                self._emit("on_unknown_message", message_type)
                ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.exception("Error processing client message: %s", e)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def _execute_client_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        work_dir: pathlib.Path,
    ) -> dict[str, Any]:
        """Run one of the tools the agent marked default_target="client".
        The agent reads the "result" key; the UI reads stdout/stderr/returncode."""
        command = str(arguments.get("command") or "")
        timeout = int(arguments.get("timeout") or DEFAULT_COMMAND_TIMEOUT)
        limit = min(int(arguments.get("limit") or DEFAULT_OUTPUT_LIMIT), MAX_OUTPUT_CHARS)

        if name == "run_in_terminal":
            return self._launch_in_terminal(command, work_dir)
        if name == "run_bash":
            argv = [_find_bash(), "-lc", command]
        elif name == "run_powershell":
            argv = [_find_powershell(), "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            return _tool_error(f"[unknown client tool: {name}]")

        try:
            result = subprocess.run(
                argv,
                cwd=work_dir,
                capture_output=True,
                text=True,
                # Both shells we launch speak UTF-8. Without saying so, Python
                # decodes with the system code page — on a Russian Windows that
                # turned "июл 28" into "РёСЋР» 28" in everything the agent read.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=NO_WINDOW,
                # Nobody is at a keyboard here. Without this the child inherits
                # whatever handle the client happens to have: an invalid one in
                # the GUI build (instant EOF), a real terminal in a console run
                # — where a script waiting for input would hang the whole
                # command until it timed out.
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return _tool_error(f"[{name} unavailable: {argv[0]} not found]")
        except subprocess.TimeoutExpired:
            return _tool_error(f"[{name}: timed out after {timeout} s]")

        output = _strip_ansi(result.stdout + result.stderr).strip()
        output = output or f"(код возврата {result.returncode})"
        if result.returncode != 0:
            output = f"[exit {result.returncode}] {output}"
        return {
            "result": _truncate_middle(output, limit),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }

    def _launch_in_terminal(self, command: str, work_dir: pathlib.Path) -> dict[str, Any]:
        """Open a real console window and hand it to the user.

        Nothing is captured and nothing is waited for: the program is theirs to
        interact with from here on. This is the only way an interactive script
        can run at all — every other command executes with no terminal, where
        the first prompt for input hits EOF.
        """
        if not command:
            return _tool_error("[run_in_terminal: пустая команда]")

        try:
            if platform.system() == "Windows":
                # Passed as one raw command line on purpose: given a list, Python
                # escapes inner quotes the C way (\") and cmd does not read them
                # back, so `python "app with space.py"` and `python -c "..."`
                # both break. `pause` keeps the window up after the program
                # exits, so its last output stays readable.
                subprocess.Popen(
                    f'cmd /c "{command} & echo. & pause"',
                    cwd=work_dir,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    close_fds=True,
                )
            else:
                terminal = next(
                    (
                        found
                        for found in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")
                        if shutil.which(found)
                    ),
                    None,
                )
                if terminal is None:
                    return _tool_error("[run_in_terminal: не найден эмулятор терминала]")
                subprocess.Popen(
                    [terminal, "-e", f"sh -c '{command}; read -p \"\"'"],
                    cwd=work_dir,
                    close_fds=True,
                )
        except Exception as error:
            return _tool_error(f"[run_in_terminal: не удалось запустить: {error}]")

        message = f"Запущено в отдельном окне терминала: {command}"
        return {"result": message, "stdout": message, "stderr": "", "returncode": 0}

    def _ask_user(self, question: str) -> dict[str, Any]:
        handler = getattr(self.event_handler, "request_user_answer", None)
        if handler is None:
            return _tool_error("[ask_user unavailable: this client cannot show questions]")
        answer = str(handler(question) or "").strip()
        if not answer:
            return _tool_error("[the user did not answer the question]")
        return {"result": answer, "stdout": answer, "stderr": "", "returncode": 0}

    def _publish(self, message: dict) -> None:
        self.allow_commands_for_request = False
        self.publish_message(message)
        self._emit("on_waiting_result")

    def _reconnect_to_personal(self, credentials: dict):
        self.connection.close()

        # The personal queue normally lives on the same machine the client just
        # logged in to, so reuse that address; the server only names a host when
        # its queues really sit elsewhere.
        host = str(credentials.get("rabbitmq_host") or self._router_host)
        port = int(credentials["rabbitmq_port"])
        personal_url = (
            f"amqp://{credentials['rabbitmq_user']}:{credentials['rabbitmq_password']}"
            f"@{host}:{port}/"
        )

        self.connection = pika.BlockingConnection(pika.URLParameters(personal_url))
        self.channel = self.connection.channel()

        self.exchange = AGENT_EXCHANGE
        self.queue = CLIENT_QUEUE
        self.routing_key = AGENT_ROUTING_KEY
        self._rpc_user = credentials["rabbitmq_user"]
        self._rpc_password = credentials["rabbitmq_password"]
        self._rpc_host = host
        self._rpc_port = port

        # Consuming is started by start_consuming() in the consumer thread.
        # Subscribing here too left two consumers on one queue, so deliveries
        # were split between them round-robin for no reason. Nothing is lost in
        # the gap: client_queue is durable and holds messages until then.
        logger.info("Connected to personal RabbitMQ at %s:%s", host, port)

    def send_logout(self):
        if not self.login:
            return
        temp_connection = None
        try:
            router_url = f"amqp://{USER}:{PASSWORD}@{self._router_host}:{self._router_port}/"
            temp_connection = pika.BlockingConnection(pika.URLParameters(router_url))
            temp_channel = temp_connection.channel()

            temp_channel.basic_publish(
                exchange=EXCHANGE,
                routing_key=ROUTING_KEY,
                body=json.dumps(
                    {
                        "type": "logout",
                        "login": self.login,
                    },
                    ensure_ascii=False,
                ),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                ),
            )
            logger.info("Logout message sent to router")
        except Exception as e:
            logger.error(f"Failed to send logout message: {e}")
        finally:
            if temp_connection and not temp_connection.is_closed:
                temp_connection.close()

    def _can_run_command(self, command: dict) -> bool:
        access = self.agent_session.get("access") or ACCESS_ASK
        if access == ACCESS_READ_ONLY:
            return False
        if access == ACCESS_FULL or self.allow_commands_for_request:
            return True

        handler = getattr(self.event_handler, "request_command_permission", None)
        decision = handler(command) if handler is not None else "deny"
        if decision == "allow_all":
            self.allow_commands_for_request = True
            return True
        return decision == "allow"

    def _supervisor_request(self, message: dict, timeout: float = 15.0) -> dict:
        rabbitmq_url = (
            f"amqp://{self._rpc_user}:{self._rpc_password}@{self._rpc_host}:{self._rpc_port}/"
        )
        connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
        channel = connection.channel()
        try:
            reply_queue = channel.queue_declare(
                queue="", exclusive=True, auto_delete=True
            ).method.queue
            correlation_id = str(uuid.uuid4())
            channel.basic_publish(
                exchange=self.exchange,
                routing_key=SUPERVISOR_ROUTING_KEY,
                body=json.dumps(message, ensure_ascii=False),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    correlation_id=correlation_id,
                    reply_to=reply_queue,
                ),
            )

            deadline = time.monotonic() + timeout
            for method, properties, body in channel.consume(
                queue=reply_queue,
                auto_ack=True,
                inactivity_timeout=timeout,
            ):
                if method is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Supervisor request timed out")
                    continue

                if properties.correlation_id != correlation_id:
                    continue

                response = json.loads(body.decode("utf-8"))
                if response.get("error"):
                    raise RuntimeError(str(response["error"]))
                return response

            raise TimeoutError("Supervisor request timed out")
        finally:
            connection.close()
