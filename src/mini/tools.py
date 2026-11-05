"""Five local tools (read, write, edit, bash, find) plus the approval gate.

A tool is a JSON schema the model sees and a Python function the harness runs.
`edit` refuses to touch a file that changed since the model last read it.
"""
import base64
import contextlib
import difflib
import fnmatch
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import session

MAX_OUT = 30_000  # characters returned to the model per tool call
WINDOWS = os.name == "nt"
WIN_RUNS = {".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".msi"}  # Windows has no execute bit: these run by name


def find_bash() -> str | None:
    """The model writes bash. POSIX: bash from PATH, since /bin/sh is dash on Debian and Ubuntu. Windows: Git for
    Windows' bash (or MINI_BASH), at fixed paths, since a lookup there searches the project folder first.
    Absolute paths only: a relative PATH entry is the project folder, which the project controls."""
    tries = [os.environ.get("MINI_BASH", ""), os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Git\bin\bash.exe"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Git\bin\bash.exe")] if WINDOWS else [shutil.which("bash") or ""]
    return next((p for p in tries if os.path.isabs(p) and os.path.isfile(p)), None)


BASH = find_bash()  # None: /bin/sh on POSIX, cmd.exe on Windows
# Image limits, measured on the Claude API (2026-11-05, Haiku 5.5): one image may be 8000 px a side, but a request
# holding over 20 images refuses any past 3000 px ("max allowed size for many-image requests: 3000 pixels"); single
# images of 6.0 and 10.1 MB base64 and a 39.6 MB request went through. A transcript can't drop an image once sent, so
# read stays under limits every later request must meet: 3000 px, 5 MB a file, 20 MB of images held.
IMAGE_PX, IMAGES_HELD = 3000, 20_000_000


class ToolError(Exception):
    pass


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # terminal control codes; keeps \n, \t and readable Unicode


def printable(text: str, lines: bool = False) -> str:
    """Nothing can hide in a prompt: padding is counted, everything outside plain ASCII is escaped, and real line
    breaks are kept only where asked (file content)."""
    text = re.sub(" {16,}" if lines else " {3,}", lambda m: f"[{len(m[0])} spaces]", text)  # code keeps its indent
    text = re.sub(r"(\n[ \t]*){3,}", lambda m: f"\n[{m[0].count(chr(10)) - 1} blank lines]\n", text) if lines else text
    return "".join(c if c.isascii() and c.isprintable() and c != "\\" or lines and c == "\n"
                   else c.encode("unicode_escape").decode() for c in text)


def run_shell(command: str, cwd, timeout: int):
    """Run a shell command with no stdin; on timeout or Ctrl-C kill its whole process group (tree, on Windows)."""
    proc = subprocess.Popen([BASH, "-c", command] if BASH else command, shell=not BASH, cwd=cwd,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PAGER": "cat"}, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        if WINDOWS:  # no process groups; taskkill by full path, since a bare name would search the project first
            subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), r"System32\taskkill.exe"),
                            "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError):  # already gone
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    return proc.returncode, out, err


def start_job(command: str, cwd) -> str:
    """Detached in its own session, so neither the call's timeout nor Ctrl-C kills it; the log says when it ends."""
    if WINDOWS:
        raise ToolError("background isn't supported on Windows: run it without background, or ask the user to start it.")
    log = session.HOME / "jobs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(6)}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as out:
        # A subshell, so `exit 3` still logs [exit 3]; `set --` inside, so the command doesn't see itself as $1.
        proc = subprocess.Popen([BASH or "/bin/sh", "-c", '(eval "set --\n$1"); echo "[exit $?]"', "job", command], cwd=cwd,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env={**os.environ, "PAGER": "cat"}, start_new_session=True)
    return (f"Started in the background: pid {proc.pid}, output in {log} (it ends with [exit N] when the job is done). "
            f"Check it with tail {log} or kill -0 {proc.pid}; stop it with kill -- -{proc.pid}.")


def image_info(data: bytes) -> tuple[str, int, int] | None:
    """(media type, width, height) from the header, for the formats the API takes; 0x0 if the size can't be read."""
    le, be = (lambda a, b: int.from_bytes(data[a:b], "little")), (lambda a, b: int.from_bytes(data[a:b], "big"))
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", be(16, 20), be(20, 24)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", le(6, 8), le(8, 10)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # three layouts: extended, lossy, lossless
        if data[12:16] == b"VP8X":
            return "image/webp", le(24, 27) + 1, le(27, 30) + 1
        if data[12:16] == b"VP8 ":
            return "image/webp", le(26, 28) & 0x3FFF, le(28, 30) & 0x3FFF
        if data[12:16] == b"VP8L":
            return "image/webp", (le(21, 25) & 0x3FFF) + 1, (le(21, 25) >> 14 & 0x3FFF) + 1
        return "image/webp", 0, 0
    if data[:3] != b"\xff\xd8\xff":
        return None
    i = 2
    while i + 9 <= len(data) and data[i] == 0xFF:  # JPEG: walk the segments to the start-of-frame, which has the size
        m = data[i + 1]
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            return "image/jpeg", be(i + 7, i + 9), be(i + 5, i + 7)
        i += 1 if m == 0xFF else 2 + be(i + 2, i + 4)  # 0xFF: a fill byte
    return "image/jpeg", 0, 0


def truncate(text: str) -> str:
    h = MAX_OUT // 2
    return text if len(text) <= MAX_OUT else f"{text[:h]}\n\n[... {len(text) - MAX_OUT} characters cut ...]\n\n{text[-h:]}"


def loose_edit(text: str, old: str, new: str) -> tuple[str | None, int]:
    """Retry whole lines, ignoring line endings and trailing whitespace: models copy those wrong, mostly on Windows.
    Returns the edited text if exactly one place matches, and how many places did."""
    lines = text.splitlines(keepends=True)
    want = [x.rstrip() for x in old.replace("\r\n", "\n").rstrip("\n").split("\n")]
    if not any(want):
        return None, 0
    hits = [i for i in range(len(lines) - len(want) + 1) if [x.rstrip() for x in lines[i:i + len(want)]] == want]
    if len(hits) != 1:
        return None, len(hits)
    i, j = hits[0], hits[0] + len(want)
    eol = "\r\n" if lines[i].endswith("\r\n") else "\n"
    end = lines[j - 1][len(lines[j - 1].rstrip("\r\n")):]  # the last line keeps its own ending, if it had one
    body = eol.join(new.replace("\r\n", "\n").rstrip("\n").split("\n")) + end if new else ""
    return "".join(lines[:i]) + body + "".join(lines[j:]), 1


def closest(text: str, old: str) -> str:
    """Point at the line most like old's first line, so the model sees what differs instead of guessing."""
    first = next((x.strip() for x in old.split("\n") if x.strip()), "")
    lines = [x.strip() for x in text.splitlines()]
    best = difflib.get_close_matches(first, lines, n=1, cutoff=0.6)
    return f" Closest: line {lines.index(best[0]) + 1}: {best[0]!r}" if best else ""


class Approver:
    """Asks before risky calls. `auto=True` approves everything (--permissions yes)."""

    # Commands that can't run other programs or write files, whatever their flags.
    # grep isn't here: `grep -r` reads every file, secrets included. The find tool searches without a shell.
    SAFE = {"ls", "cat", "head", "tail", "wc", "pwd", "echo", "which", "stat", "diff"}
    # Read-only git. It runs only what git config says, and writes into .git need approval; `--output` writes,
    # and a `rev:path` word could show a committed secret, so those go to review.
    GIT = {"status", "diff", "log", "show"}
    SHELL = set(";&|<>`$(){}\n\r\\")

    def __init__(self, auto: bool = False, interactive: bool = True, allow: tuple = (), use_judge: bool = False):
        self.auto, self.interactive = auto, interactive
        self.use_judge, self.judge, self.reason = use_judge, None, ""  # judge(kind, detail) -> (allow|deny|ask, why)
        self.allow = [shlex.split(a) for a in allow if a.strip()]

    @property
    def mode(self) -> str:
        return "yes" if self.auto else "judge" if self.use_judge else "ask"

    def pre_approved(self, command: str, private=False) -> bool:
        if self.SHELL & set(command):
            return False
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        git = words[:1] == ["git"] and words[1:2] and words[1] in self.GIT and not any(
            w.startswith(("--output", "--ext-diff", "--textconv")) or ":" in w for w in words)
        return bool(words) and (((words[0] in self.SAFE or git) and not private)
                                or any(words[:len(a)] == a for a in self.allow))

    def ok(self, kind: str, detail: str, private=False, secret=False) -> bool:
        if self.auto or (kind == "bash" and self.pre_approved(detail, private)):
            return True
        if kind == "mcp" and any(detail.split()[0].startswith(a[0]) for a in self.allow):
            return True
        self.reason = ""
        if len(detail) > 4000:  # too long to review, by you or the judge, which must see the whole action
            self.reason = " Too long to review: split it into smaller steps."
            return False
        if self.use_judge and self.judge and kind != "write" and not secret:  # gated writes, secrets: the user decides
            verdict, why = self.judge(kind, detail)
            self.reason = f" Judge: {why}"
            if verdict != "ask":
                return verdict == "allow"
        if not self.interactive:
            self.reason += (" Nobody can approve it in this run, so it was denied: try a smaller step, stay inside "
                            "the project, or ask the user to rerun with --allow.")
            return False
        # Every row carries a marker content can't fake, and nothing taller than a screen is shown at all.
        cols = max(shutil.get_terminal_size().columns - 6, 20)
        rows = [r[i:i + cols] for r in printable(detail, kind == "write").split("\n") for i in range(0, len(r) or 1, cols)]
        if len(rows) > max(min(40, shutil.get_terminal_size().lines - 3), 10):  # the top must stay on screen
            self.reason = " Too long to review on one screen: split it, or write a short file and extend it with edit."
            return False
        head = printable(detail.split("\n", 1)[0] if kind == "write" else kind)  # the path, by the answer
        return input(f"\033[!p\0337\033[r\0338\033[0m\033[?7h\033[33m  allow {kind}:" + "".join(f"\n  \u2502 {r}" for r in rows)
                     + f"\n  [{head} · {len(rows)} rows · y/n] \033[0m").strip().lower() in ("y", "yes")


class LocalTools:
    def __init__(self, cwd: str, approver: Approver):
        self.cwd = Path(cwd).resolve()
        self.approver = approver
        self.seen: dict[Path, float] = {}  # path -> mtime when the model last read or wrote it
        self.jobs = (session.HOME / "jobs").resolve()  # background job logs: start_job writes them
        self.held = 0  # base64 bytes of images in the transcript; the Agent resets it on resume and compaction

    def _path(self, p: str) -> Path:
        if WINDOWS and re.match(r"/[a-zA-Z](/|$)", p):  # Git Bash spells C:\x as /c/x
            p = f"{p[1]}:/{p[3:]}"
        try:
            path = Path(p).expanduser()
        except RuntimeError:  # ~word for no known user: a literal name, as bash keeps it
            path = Path(p)
        return (path if path.is_absolute() else self.cwd / path).resolve()

    # Files something else executes later; "head" and "config" because git treats any folder with
    # HEAD, config, objects/ and refs/ as a repository and runs hooks from its config.
    # This is a denylist: it covers common runners, not every tool that reads a file from the project.
    PROTECTED = {"mcp.json", "conftest.py", "makefile", "gnumakefile", "justfile", "package.json", "pyproject.toml",
                 "setup.py", "noxfile.py", "tox.ini", "head", "config"}

    def _inside(self, path: Path) -> bool:
        """Free to write: inside the working directory, and not a file something else will execute later
        (dot-folders such as .git hooks and config, MCP config, test or build config). macOS paths ignore case."""
        home = Path.home().resolve()
        if self.cwd not in path.parents or self.cwd == home or self.cwd in home.parents:
            return False
        parts = [x.casefold() for x in path.relative_to(self.cwd).parts]
        if WINDOWS and any(re.search(r"[:~]|[. ]$", x) for x in parts):  # streams, 8.3 names, trailing dots alias files
            return False
        return not any(x.startswith(".") for x in parts) and parts[-1] not in self.PROTECTED

    # Files that usually hold keys or passwords, and folders of them. Templates (.env.example) are fine.
    SECRET = re.compile(r"^\.env(?!\.(example|sample|template)$)|\.(pem|key|p12|pfx|jks|keystore)$|^id_(rsa|dsa|ecdsa|ed25519)"
                        r"|^(\.netrc|\.npmrc|\.pypirc|\.git-credentials|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$")
    SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", "gcloud"}

    def _secret(self, path: Path) -> bool:
        parts = [x.casefold() for x in path.parts]
        return bool(self.SECRET_DIRS.intersection(parts) or self.SECRET.search(parts[-1] if parts else ""))

    def _private(self, path: Path) -> bool:
        """Reading asks first outside the working directory (except background job logs), and for files that may
        hold secrets anywhere: whatever the model reads goes to the API and into the session file."""
        outside = self.cwd != path and self.cwd not in path.parents and self.jobs not in path.parents
        return outside or self._secret(path)  # job logs hold the output of commands that were already approved

    def _words(self, command: str) -> list[Path] | None:
        """The paths a command names (every word after the first that isn't a flag); None if it can't be split."""
        try:
            if os.name == "nt":  # shlex eats the backslashes in C:\\x\\.env, hiding the secret's name: read them as /
                command = command.replace("\\", "/")
            return [self._path(w) for w in shlex.split(command)[1:] if not w.startswith("-")]
        except ValueError:
            return None

    def _names_private(self, command: str) -> bool:
        """Does a command name a path outside the project, a secret file, or a glob that could match one?"""
        paths = self._words(command)
        return paths is None or bool(re.search(r"[*?\[]", command)) or any(self._private(p) for p in paths)

    def _ask(self, path: Path) -> bool:
        """Writes need approval outside the project, into protected files, or over an executable."""
        runs = path.suffix.casefold() in WIN_RUNS if WINDOWS else os.access(path, os.X_OK)  # Windows: every file is X_OK
        return not self._inside(path) or (path.exists() and runs)

    def read(self, path: str, offset: int = 1, limit: int = 2000) -> str | list:
        p = self._path(path)
        if self._private(p) and not self.approver.ok("read", str(p), secret=self._secret(p)):
            raise ToolError("User denied this read (outside the project, or a file that may hold secrets)."
                            + self.approver.reason)
        if p.is_dir():
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))
        if not p.is_file():
            raise ToolError("Not a regular file.")
        if p.stat().st_size > 5_000_000:
            raise ToolError("File is over 5 MB. Use bash (head, tail, grep) to look at parts of it; for an image, "
                            "make a smaller copy.")
        data = p.read_bytes()
        self.seen[p] = p.stat().st_mtime
        if (image := image_info(data)):
            kind, w, h = image
            if not w or not h or max(w, h) > IMAGE_PX:
                raise ToolError(f"Can't show this image ({f'{w}x{h} px' if w and h else 'size unreadable'}): images go "
                                f"to the model only up to {IMAGE_PX} px on a side. Make a smaller copy with bash and read "
                                f"that (macOS: sips -Z {IMAGE_PX} <in> --out <out>; elsewhere ImageMagick or Pillow).")
            b64 = base64.b64encode(data).decode()
            if self.held + len(b64) > IMAGES_HELD:  # every request sends them all again
                raise ToolError(f"This conversation already holds {self.held / 1e6:.1f} MB of images, and mini stops at "
                                f"{IMAGES_HELD // 1_000_000} MB. Ask the user to /compact or start a new session, or make "
                                f"a smaller copy (fewer pixels, or JPEG) and read that.")
            self.held += len(b64)
            return [{"type": "text", "text": f"{p.name}: {kind}, {w}x{h} px"},
                    {"type": "image", "source": {"type": "base64", "media_type": kind, "data": b64}}]
        lines = data.decode("utf-8", errors="replace").splitlines()
        start, shown, size = max(offset, 1), [], 0
        for i, line in enumerate(lines[start - 1:start - 1 + max(limit, 1)], start):
            numbered = f"{i:>5}\t{line}"
            if shown and size + len(numbered) > MAX_OUT - 200:  # page here, never let the output cap cut the middle
                break
            shown.append(numbered)
            size += len(numbered) + 1
        end = start + len(shown) - 1
        more = f"\n[lines {start}-{end} of {len(lines)}; continue with offset={end + 1}]" if end < len(lines) else ""
        return "\n".join(shown) + more

    def write(self, path: str, content: str) -> str:
        p = self._path(path)
        if p.exists() and p.stat().st_mtime != self.seen.get(p):
            raise ToolError("File exists and you haven't read its current version. Read it first, or use edit.")
        if self._ask(p) and not self.approver.ok("write", f"{p}\n{content}"):
            raise ToolError("User denied this write (outside the project, or a file that runs later)." + self.approver.reason)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            with p.open(newline="", encoding="utf-8", errors="replace") as f:
                if "\r\n" in f.read():  # keep a Windows file's line endings
                    content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        p.write_text(content, newline="", encoding="utf-8")
        self.seen[p] = p.stat().st_mtime
        return f"Wrote {len(content.splitlines())} lines to {p}"

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = self._path(path)
        if p not in self.seen:
            raise ToolError("Read the file with the read tool before editing it (bash output doesn't count).")
        if p.stat().st_mtime != self.seen[p]:
            raise ToolError("File changed since you last read it. Read it again.")
        if self._ask(p) and not self.approver.ok("write", "\n".join(
                [str(p)] + ["- " + x for x in old.split("\n")] + ["+ " + x for x in new.split("\n")])):
            raise ToolError("User denied this edit (outside the project, or a file that runs later)." + self.approver.reason)
        if not old:
            raise ToolError("`old` must not be empty.")
        with p.open(newline="", encoding="utf-8") as f:  # keep the file's own line endings
            text = f.read()
        if old not in text and "\r\n" in text:  # the model writes \n; the file uses \r\n
            old = old.replace("\r\n", "\n").replace("\n", "\r\n")
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        n = text.count(old)
        if n == 0:
            done, hits = loose_edit(text, old, new)
            if done is not None:
                p.write_text(done, newline="", encoding="utf-8")
                self.seen[p] = p.stat().st_mtime
                return f"Edited {p} (1 replaced, ignoring line endings and trailing whitespace)"
            raise ToolError(f"`old` matches {hits} places once whitespace is ignored. Add context." if hits else
                            "`old` not found. It must match the file exactly, including whitespace." + closest(text, old))
        if n > 1 and not replace_all:
            raise ToolError(f"`old` appears {n} times. Add context to make it unique, or set replace_all.")
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), newline="", encoding="utf-8")
        self.seen[p] = p.stat().st_mtime
        return f"Edited {p} ({n if replace_all else 1} replaced)"

    def find(self, name: str = "*", contains: str = "", path: str = ".") -> str:
        """Search with no shell. Asks only outside the project; skips dot-folders, dependency folders and secrets."""
        root, rx, hits, size = self._path(path), re.compile(contains) if contains else None, [], 0  # bad regex: re.error
        if "/" in name or not root.exists():
            raise ToolError("`name` matches file names only; put the folder in `path`." if "/" in name else "No such path.")
        if self._private(root) and not self.approver.ok("find", f"{root} · name {name} · contains {contains or '-'}",
                                                        secret=self._secret(root)):
            raise ToolError("User denied this search (outside the project, or a file that may hold secrets)."
                            + self.approver.reason)
        end, walk = time.monotonic() + 30, os.walk(root) if root.is_dir() else [(root.parent, [], [root.name])]
        for d, dirs, files in walk:
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__"))
            for p in (Path(d, f) for f in sorted(fnmatch.filter(files, name))):
                shown = (os.path.relpath(p, self.cwd) if self.cwd in p.parents else str(p)).replace(os.sep, "/").replace("\n", "\\n")
                if rx and p != root and self._private(p):  # names are listed; secrets' contents aren't searched
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace") if rx and p.is_file() and p.stat().st_size < 5_000_000 else ""
                except OSError:  # unreadable: skip it, keep the rest
                    continue
                if not rx or "\0" not in text[:8000]:  # skip binaries
                    new = [f"{shown}:{i}: {x[:300]}" for i, x in enumerate(text.splitlines(), 1)
                           if rx.search(x)] if rx else [shown]
                    hits, size = hits + new, size + sum(len(x) + 1 for x in new)
                if len(hits) >= 500 or size > MAX_OUT - 200 or time.monotonic() > end:
                    return ("\n".join(hits[:500])[:MAX_OUT - 200]
                            + "\n[stopped early (500 results, 30,000 characters or 30 s): narrow the search]")
        return "\n".join(hits) or "[no matches]"

    def bash(self, command: str, timeout: int = 120, background: bool = False) -> str:
        secret = any(self._secret(p) for p in self._words(command) or [])  # names a secret file: you decide
        if not self.approver.ok("bash", command, self._names_private(command), secret):
            raise ToolError("User denied this command." + self.approver.reason)
        if background:
            return start_job(command, self.cwd)
        try:
            code, stdout, stderr = run_shell(command, self.cwd, max(timeout, 1))
        except subprocess.TimeoutExpired:
            raise ToolError(f"Timed out after {timeout}s.")
        out = (stdout + (("\n[stderr]\n" + stderr) if stderr else "")).strip()
        return f"{out}\n[exit {code}]" if code else (out or "[no output]")


def schema(props: dict, required: list) -> dict:
    """strict: the API guarantees inputs match the schema, so calls never fail on argument shape."""
    return {"strict": True, "input_schema": {"type": "object", "properties": props, "required": required,
                                             "additionalProperties": False}}


SCHEMAS = {
    "read": {
        "description": "Read a text file or an image (PNG, JPEG, GIF or WebP, up to 3000 px on a side: you see the "
                       "picture), or list a directory. Paths are relative to the working directory. Output lines start "
                       "with a line number and a tab; that prefix is not part of the file. Long files come in pages of "
                       "about 30,000 characters; the last line says which offset to pass next. Reading outside the "
                       "working directory, or a file that may hold secrets (.env, keys, credentials), goes through "
                       "approval (see the system prompt); don't read secrets unless the task needs them.",
        **schema({"path": {"type": "string"},
                  "offset": {"type": "integer", "description": "First line to show, 1-based (default 1)"},
                  "limit": {"type": "integer", "description": "Lines to show (default 2000)"}}, ["path"])},
    "write": {
        "description": "Create a file, or replace a whole file. To overwrite an existing file, read it first; for "
                       "small changes use edit. Writing outside the working directory, into dot-folders, or to "
                       "files that run later (Makefile, conftest.py, mcp.json, executables) goes through approval.",
        **schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])},
    "edit": {
        "description": "Replace exact text in a file you have read since it last changed. `old` must match the file "
                       "character for character, including indentation, and must not include the line-number "
                       "prefixes that read shows. It must occur once, unless replace_all is true; add surrounding "
                       "lines to make it unique.",
        **schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                  "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"}},
                 ["path", "old", "new"])},
    "bash": {
        "description": "Run a shell command in the working directory and get stdout, stderr and the exit code. Use "
                       "it for git, tests and builds; search files with the find tool instead. There is no terminal: "
                       "commands get no input, so interactive programs end at once. Output over 30,000 characters is cut in the "
                       "middle. The call waits while anything holds the command's output open, so a job left running "
                       "with & keeps it waiting, and a timeout kills the job with its whole process group. For servers, "
                       "long builds and other long jobs, set background: the command starts detached, writes to a log "
                       "file, and the call returns at once with the pid and the log's path. Commands other than plain reads "
                       "(ls, cat or head of project files, read-only git; no pipes or redirects) go through approval.",
        **schema({"command": {"type": "string"},
                  "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 120)"},
                  "background": {"type": "boolean", "description": "Start it detached and return at once (default false)"}},
                 ["command"])},
    "find": {
        "description": "Find files by name, or lines by content (path:line: text), in a folder or one file. Read-only; "
                       "searching outside the working directory goes through approval. Paths come back relative to the working directory. Skips dot-folders, "
                       "node_modules and __pycache__, symlinked folders, and the contents of files that may hold secrets. Stops at 500 results or 30 seconds.",
        **schema({"name": {"type": "string",
                           "description": "Glob matched against file names only (no /), such as *.py; default *"},
                  "contains": {"type": "string", "description": "Python regex to match lines; omit to list files"},
                  "path": {"type": "string", "description": "Folder or file to search (default the working directory)"}},
                 [])},
}

# Who approves, said once in the system prompt (and again in a note when it changes): a model told that it may be
# denied believes it, even when everything is approved.
GATED = ("bash commands other than plain reads, reads and searches outside the working directory or of files that may "
         "hold secrets, writes outside it or to files that run later, and MCP calls")


def approval(ap: Approver) -> str:
    if ap.auto:
        return "Approval in this session (permissions yes): every tool call runs without asking, so nothing is denied."
    nobody = "are denied, since nobody can approve in this run"
    how = (f"go to a reviewer model that may say no; writes, secrets and calls it's unsure about "
           f"{'go to the user' if ap.interactive else nobody}") if ap.use_judge else (
        "ask the user, who may say no" if ap.interactive else nobody)
    pre = ", ".join(shlex.join(a) for a in ap.allow)
    return (f"Approval in this session (permissions {ap.mode}): {GATED} {how}."
            + (f" The user pre-approved calls starting with: {pre}." if pre else ""))


# Direct calls only: from 20260209 on, the default runs searches from Anthropic's code-execution sandbox, which comes
# along unasked, and the model then takes that sandbox for the user's machine.
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]}
