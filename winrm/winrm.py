#!/usr/bin/env python3

import argparse
import re
import signal
import sys
import textwrap
import traceback
from pathlib import Path

from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import clear
from pypsrp.complex_objects import PSInvocationState
from pypsrp.exceptions import AuthenticationError, WinRMTransportError, WSManFaultError
from pypsrp.powershell import PowerShell, RunspacePool
from requests.exceptions import ConnectionError
from spnego.exceptions import NoCredentialError, OperationNotAvailableError, SpnegoError

# check if kerberos is installed
try:
    from gssapi.creds import Credentials as GSSAPICredentials
    from gssapi.exceptions import ExpiredCredentialsError, MissingCredentialsError
    from gssapi.raw import Creds as RawCreds
    from krb5._exceptions import Krb5Error

    is_kerb_available = True
except ImportError:
    is_kerb_available = False

    # If kerberos is not available, define a dummy exception
    class Krb5Error(Exception):
        pass


from winrm import __version__
from winrm.pypsrp_ewp.wsman import WSManEWP

# --- Constants ---
HISTORY_FILE = Path.home().joinpath(".winrm")
HISTORY_LENGTH = 1000

# --- Colors ---
# ANSI escape codes for colored output
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BOLD = "\033[1m"


# --- Helper Functions ---
class DelayedKeyboardInterrupt:
    """
    A context manager to delay the handling of a SIGINT (Ctrl+C) signal until
    the enclosed block of code has completed execution.

    This is useful for ensuring that critical sections of code are not
    interrupted by a keyboard interrupt, while still allowing the signal
    to be handled after the block finishes.
    """

    def __enter__(self):
        self.signal_received = False
        self.old_handler = signal.getsignal(signal.SIGINT)

        def handler(sig, frame):
            print(RED + "\n[-] Caught Ctrl+C. Stopping current command..." + RESET)
            self.signal_received = (sig, frame)

        signal.signal(signal.SIGINT, handler)

    def __exit__(self, type, value, traceback):
        signal.signal(signal.SIGINT, self.old_handler)
        if self.signal_received:
            # raise the signal after the task is done
            self.old_handler(*self.signal_received)


def run_ps_cmd(r_pool: RunspacePool, command: str) -> tuple[str, list, bool]:
    """Runs a PowerShell command and returns the output, streams, and error status."""
    ps = PowerShell(r_pool)
    ps.add_cmdlet("Invoke-Expression").add_parameter("Command", command)
    ps.add_cmdlet("Out-String").add_parameter("Stream")
    ps.invoke()
    return "\n".join(ps.output), ps.streams, ps.had_errors


def get_prompt(r_pool: RunspacePool) -> str:
    """Returns the prompt string for the interactive shell."""
    output, streams, had_errors = run_ps_cmd(
        r_pool, "$pwd.Path"
    )  # Get current working directory
    if not had_errors:
        return f"{YELLOW}{BOLD}PS{RESET} {output}> "
    return "PS ?> "  # Fallback prompt


def get_directory_and_partial_name(text: str, sep: str) -> tuple[str, str]:
    """
    Parses the input text to find the directory prefix and the partial name.
    """
    if sep not in ["\\", "/"]:
        raise ValueError("Separator must be either '\\' or '/'")
    # Find the last unquoted slash or backslash
    last_sep_index = text.rfind(sep)
    if last_sep_index == -1:
        # No separator found, the whole text is the partial name in the current directory
        directory_prefix = ""
        partial_name = text
    else:
        split_at = last_sep_index + 1
        directory_prefix = text[:split_at]
        partial_name = text[split_at:]
    return directory_prefix, partial_name


def _ps_single_quote(value: str) -> str:
    """Wraps a value in single quotes for PowerShell, escaping embedded quotes."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def get_remote_path_suggestions(
    r_pool: RunspacePool,
    directory_prefix: str,
    partial_name: str,
    dirs_only: bool = False,
) -> list[str]:
    """
    Returns a list of remote path suggestions based on the current directory
    and the partial name entered by the user.
    """

    exp = "FullName"
    attrs = ""
    if not re.match(r"^[a-zA-Z]:", directory_prefix):
        # If the path doesn't start with a drive letter, prepend the current directory
        pwd, streams, had_errors = run_ps_cmd(
            r_pool, "$pwd.Path"
        )  # Get current working directory
        directory_prefix = f"{pwd}\\{directory_prefix}"
        exp = "Name"

    if dirs_only:
        attrs = "-Attributes Directory"

    command = f'Get-ChildItem -LiteralPath "{directory_prefix}" -Filter "{partial_name}*" {attrs} -Fo | select -Exp {exp}'
    ps = PowerShell(r_pool)
    ps.add_cmdlet("Invoke-Expression").add_parameter("Command", command)
    ps.add_cmdlet("Out-String").add_parameter("Stream")
    ps.invoke()
    return ps.output


def get_remote_command_suggestions(
    r_pool: RunspacePool, command_prefix: str
) -> list[str]:
    """
    Returns a list of remote PowerShell command names (cmdlets/aliases) that start
    with the provided prefix.
    """

    prefix_literal = _ps_single_quote(command_prefix or "")
    ps_script = textwrap.dedent(
        f"""
        $prefix = {prefix_literal};
        if ([string]::IsNullOrEmpty($prefix)) {{
            $pattern = '*';
        }} else {{
            $pattern = "$prefix*";
        }}
        $cmds = Get-Command -Name $pattern -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Name;
        if (-not $cmds) {{
            $cmds = Get-Alias -Name $pattern -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty Name;
        }}
        $cmds | Sort-Object -Unique
        """
    ).strip()

    output, _, had_errors = run_ps_cmd(r_pool, ps_script)
    if had_errors:
        return []
    suggestions = [line.strip() for line in output.splitlines() if line.strip()]
    return suggestions


class CommandPathCompleter(Completer):
    """
    Completer for command paths in the interactive shell.
    This completer suggests command names based on the user's input.
    """

    def __init__(self, r_pool: RunspacePool):
        self.r_pool = r_pool

    def get_completions(self, document: Document, complete_event):
        dirs_only = False  # Whether to suggest only directories
        text_before_cursor = document.text_before_cursor.lstrip()
        tokens = text_before_cursor.split(maxsplit=1)

        if not tokens:
            return

        command_typed_part = tokens[0]

        # Handle .\name or ./name as first-token paths (run from current remote directory)
        if command_typed_part.startswith(".\\") or command_typed_part.startswith("./"):
            path_being_completed = command_typed_part
            # strip surrounding quotes if any
            if path_being_completed.startswith('"') and path_being_completed.endswith(
                '"'
            ):
                path_being_completed = path_being_completed.strip('"')
            directory_prefix, partial_name = get_directory_and_partial_name(
                path_being_completed, sep="\\"
            )
            suggestions = get_remote_path_suggestions(
                self.r_pool, directory_prefix, partial_name
            )
            for sugg_path in suggestions:
                text_to_insert_in_prompt = f".\\" + sugg_path
                if " " in sugg_path:
                    text_to_insert_in_prompt = f'& ".\\{sugg_path}"'
                yield Completion(
                    text_to_insert_in_prompt,
                    start_position=-len(command_typed_part),
                    display=sugg_path,
                )
            return

        # Case 1: Completing the command name itself
        # There's only one token and no trailing space.
        if len(tokens) == 1 and not text_before_cursor.endswith(" "):
            remote_cmds = get_remote_command_suggestions(
                self.r_pool, command_typed_part
            )
            lower_prefix = command_typed_part.lower()
            for remote_cmd in remote_cmds:
                cmd_lower = remote_cmd.lower()
                if lower_prefix and not cmd_lower.startswith(lower_prefix):
                    continue
                yield Completion(
                    remote_cmd + " ",
                    start_position=-len(command_typed_part),
                    display=remote_cmd,
                )
            return

        # Case 2: Completing a path argument
        path_typed_segment = ""  # What the user has typed for the current path argument
        if len(tokens) == 2:
            path_typed_segment = tokens[1]

        actual_command_name = command_typed_part.strip().lower()

        if actual_command_name == "cd":
            dirs_only = True

        current_arg_text_being_completed = path_being_completed = path_typed_segment

        if path_being_completed.startswith('"'):
            path_being_completed = current_arg_text_being_completed.strip('"')

        directory_prefix, partial_name = get_directory_and_partial_name(
            path_being_completed, sep="\\"
        )
        suggestions = get_remote_path_suggestions(
            self.r_pool, directory_prefix, partial_name, dirs_only
        )

        for sugg_path in suggestions:
            # If the path doesn't start with a drive letter, prepend the directory_prefix
            if (
                not re.match(r"^[a-zA-Z]:", directory_prefix)
                and directory_prefix
                and directory_prefix.endswith("\\")
            ):
                sugg_path = f"{directory_prefix}{sugg_path}"

            text_to_insert_in_prompt = sugg_path

            if " " in sugg_path:
                # If the path contains spaces, quote it
                text_to_insert_in_prompt = f'"{sugg_path}"'

            yield Completion(
                text_to_insert_in_prompt,
                start_position=-len(
                    current_arg_text_being_completed
                ),  # Use the length of quoted part
                display=sugg_path,
            )


def interactive_shell(r_pool: RunspacePool) -> None:
    """Runs the interactive pseudo-shell."""
    # Set up history file
    if not HISTORY_FILE.exists():
        Path(HISTORY_FILE).touch()
    prompt_history = FileHistory(HISTORY_FILE)
    prompt_session = PromptSession(history=prompt_history)

    # Set up command completer
    completer = CommandPathCompleter(r_pool)

    # Set up key bindings
    kb = KeyBindings()

    @kb.add("enter", filter=has_completions)
    def _(event):
        """Accept the highlighted completion without executing the command."""
        event.current_buffer.apply_completion(
            event.current_buffer.complete_state.current_completion or Completion("", 0)
        )

    while True:
        try:
            try:
                prompt_text = ANSI(get_prompt(r_pool))
            except (KeyboardInterrupt, EOFError):
                return
            command = prompt_session.prompt(
                prompt_text,
                completer=completer,
                complete_while_typing=False,
                key_bindings=kb,
            )

            if not command:
                continue

            # Normalize command input
            command_lower = str(command).strip().lower()

            # Check for exit command
            if command_lower == "exit":
                return
            elif command_lower in ["clear", "cls"]:
                clear()  # Clear the screen
                continue
            else:
                try:
                    ps = PowerShell(r_pool)
                    ps.add_cmdlet("Invoke-Expression").add_parameter("Command", command)
                    ps.add_cmdlet("Out-String").add_parameter("Stream")
                    ps.begin_invoke()

                    cursor = 0
                    while ps.state == PSInvocationState.RUNNING:
                        with DelayedKeyboardInterrupt():
                            ps.poll_invoke()
                        output = ps.output
                        for line in output[cursor:]:
                            print(line)
                        cursor = len(output)

                    if ps.streams.error:
                        for error in ps.streams.error:
                            print(RED + error._to_string + RESET)
                except KeyboardInterrupt:
                    if ps.state == PSInvocationState.RUNNING:
                        ps.stop()
        except KeyboardInterrupt:
            print("\nCaught Ctrl+C. Type 'exit' or press Ctrl+D to exit.")
            continue  # Allow user to continue or type exit
        except EOFError:
            return  # Exit on Ctrl+D


# --- Main Function ---
def main():
    parser = argparse.ArgumentParser(description="WinRM client for Linux")

    parser.add_argument(
        "ip",
        help="remote host IP or hostname",
    )
    parser.add_argument("-u", "--user", help="username")
    parser.add_argument("-p", "--password", help="password")
    parser.add_argument("-H", "--hash", help="nthash")
    parser.add_argument(
        "--priv-key-pem",
        help="local path to private key PEM file",
    )
    parser.add_argument(
        "--cert-pem",
        help="local path to certificate PEM file",
    )
    parser.add_argument("--uri", default="wsman", help="wsman URI (default: /wsman)")
    parser.add_argument(
        "--ua",
        default="Microsoft WinRM Client",
        help='user agent for the WinRM client (default: "Microsoft WinRM Client")',
    )
    parser.add_argument(
        "--port", type=int, default=5985, help="remote host port (default 5985)"
    )
    if is_kerb_available:
        parser.add_argument(
            "--spn-prefix",
            help="specify spn prefix",
        )
        parser.add_argument(
            "--spn-hostname",
            help="specify spn hostname",
        )
        parser.add_argument(
            "-k", "--kerberos", action="store_true", help="use kerberos authentication"
        )
    parser.add_argument(
        "--no-pass", action="store_true", help="do not prompt for password"
    )
    parser.add_argument("--ssl", action="store_true", help="use ssl")
    parser.add_argument("--no-colors", action="store_true", help="disable colors")
    parser.add_argument("--version", action="version", version=__version__, help="show version")

    args = parser.parse_args()

    # Set Default values
    auth = "ntlm"  # this can be 'negotiate'
    encryption = "auto"
    username = args.user

    # --- Run checks on provided arguments ---
    if args.no_colors:
        global RESET, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, BOLD
        RESET = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = BOLD = ""

    if args.cert_pem or args.priv_key_pem:
        auth = "certificate"
        encryption = "never"
        args.ssl = True
        args.no_pass = True
        if not args.cert_pem or not args.priv_key_pem:
            print(
                RED
                + "[-] Both cert.pem and priv-key.pem must be provided for certificate authentication."
                + RESET
            )
            sys.exit(1)

    if args.hash and args.password:
        print(RED + "[-] You cannot use both password and hash." + RESET)
        sys.exit(1)

    if args.hash:
        ntlm_hash_pattern = r"^[0-9a-fA-F]{32}$"
        if re.match(ntlm_hash_pattern, args.hash):
            args.password = f"00000000000000000000000000000000:{args.hash}"
        else:
            print(RED + "[-] Invalid NTLM hash format." + RESET)
            sys.exit(1)

    if args.uri:
        if args.uri.startswith("/"):
            args.uri = args.uri.lstrip("/")

    if args.ssl and (args.port == 5985):
        args.port = 5986

    # --- Initialize WinRM Session ---
    try:
        if is_kerb_available:
            if args.kerberos:
                auth = "kerberos"
                args.spn_prefix = (
                    args.spn_prefix or "http"
                )  # can also be cifs, ldap, HOST
                if not args.user:
                    try:
                        cred = GSSAPICredentials(RawCreds())
                        username = cred.name
                    except MissingCredentialsError:
                        print(
                            MAGENTA
                            + "[%] No credentials cache found for Kerberos authentication."
                            + RESET
                        )
                        sys.exit(1)
                    except ExpiredCredentialsError as ece:
                        print(
                            RED + "[-] The Kerberos credentials have expired. " + RESET
                        )
                        sys.exit(1)
                # User needs to set environment variables `KRB5CCNAME` and `KRB5_CONFIG` as per requirements
                # example: export KRB5CCNAME=/tmp/krb5cc_1000
                # example: export KRB5_CONFIG=/etc/krb5.conf
            elif args.spn_prefix or args.spn_hostname:
                args.spn_prefix = args.spn_hostname = None  # Reset to None
                print(
                    MAGENTA
                    + "[%] SPN prefix/hostname is only used with Kerberos authentication."
                    + RESET
                )
        else:
            args.spn_prefix = args.spn_hostname = None

        if args.no_pass:
            args.password = None
        elif args.user and not args.password:
            args.password = prompt("Password: ", is_password=True)
            if not args.password:
                args.password = None

        with WSManEWP(
            server=args.ip,
            port=args.port,
            auth=auth,
            encryption=encryption,
            username=args.user,
            password=args.password,
            ssl=args.ssl,
            cert_validation=False,
            path=args.uri,
            negotiate_service=args.spn_prefix,
            negotiate_hostname_override=args.spn_hostname,
            certificate_key_pem=args.priv_key_pem,
            certificate_pem=args.cert_pem,
            user_agent=args.ua,
        ) as wsman:
            with RunspacePool(wsman) as r_pool:
                interactive_shell(r_pool)
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
    except WinRMTransportError as wte:
        print(RED + f"[-] {wte}" + RESET)
        sys.exit(1)
    except ConnectionError as ce:
        print(
            RED + f"[-] Failed to connect to the remote host: {args.ip}:{args.port}"
            + RESET
        )
        sys.exit(1)
    except AuthenticationError as ae:
        print(RED + f"[-] {ae}" + RESET)
        sys.exit(1)
    except WSManFaultError as wfe:
        print(RED + f"[-] {wfe}" + RESET)
        sys.exit(1)
    except Krb5Error as ke:
        print(RED + f"[-] {ke}" + RESET)
        sys.exit(1)
    except (OperationNotAvailableError, NoCredentialError) as se:
        print(RED + f"[-] {se._context_message}" + RESET)
        print(RED + f"[-] {se._BASE_MESSAGE}" + RESET)
        sys.exit(1)
    except SpnegoError as se:
        print(RED + f"[-] {se._context_message}" + RESET)
        print(RED + f"[-] {se.message}" + RESET)
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        sys.exit(1)
