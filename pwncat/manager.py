#!/usr/bin/env python3
from typing import List, Dict, Union, Optional
from prompt_toolkit.shortcuts import confirm
from io import TextIOWrapper
import contextlib
import pkgutil
import fnmatch
import selectors
import sys
import os

from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
import rich.progress

import pwncat.db
import pwncat.modules
from pwncat.util import console
from pwncat.platform import Platform
from pwncat.channel import Channel, ChannelClosed
from pwncat.config import Config
from pwncat.commands import CommandParser


class RawModeExit(Exception):
    """ Indicates that the user would like to exit the raw mode
    shell. This is normally raised when the user presses the
    <prefix>+<C-d> key combination to return to the local prompt."""


class InteractiveExit(Exception):
    """ Indicates we should exit the interactive terminal """


class Session:
    """ Wraps a channel and platform and tracks configuration and
    database access per session """

    def __init__(
        self,
        manager,
        platform: Union[str, Platform],
        channel: Optional[Channel] = None,
        **kwargs,
    ):
        self.manager = manager
        self.background = None
        self._db_session = None

        # If necessary, build a new platform object
        if isinstance(platform, Platform):
            self.platform = platform
        else:
            # If necessary, build a new channel
            if channel is None:
                channel = pwncat.channel.create(**kwargs)

            self.platform = pwncat.platform.find(platform)(
                self, channel, self.config.get("log", None)
            )

        self._progress = rich.progress.Progress(
            str(self.platform),
            "•",
            "{task.description}",
            "•",
            "{task.fields[status]}",
            transient=True,
        )

        # Register this session with the manager
        self.manager.sessions.append(self)
        self.manager.target = self

        # Initialize the host reference
        self.hash = self.platform.get_host_hash()
        with self.db as session:
            self.host = session.query(pwncat.db.Host).filter_by(hash=self.hash).first()
        if self.host is None:
            self.register_new_host()
        else:
            self.log("loaded known host from db")

    @property
    def config(self):
        """ Get the configuration object for this manager. This
        is simply a wrapper for session.manager.config to make
        accessing configuration a little easier. """
        return self.manager.config

    def register_new_host(self):
        """ Register a new host in the database. This assumes the
        hash has already been stored in ``self.hash`` """

        # Create a new host object and add it to the database
        self.host = pwncat.db.Host(hash=self.hash, platform=self.platform.name)

        with self.db as session:
            session.add(self.host)

        self.log("registered new host w/ db")

    def run(self, module: str, **kwargs):
        """ Run a module on this session """

        if module not in self.manager.modules:
            raise pwncat.modules.ModuleNotFound(module)

        if (
            self.manager.modules[module].PLATFORM is not None
            and type(self.platform) not in self.manager.modules[module].PLATFORM
        ):
            raise pwncat.modules.IncorrectPlatformError(module)

        return self.manager.modules[module].run(self, **kwargs)

    def find_module(self, pattern: str, base=None):
        """ Locate a module by a glob pattern. This is an generator
        which may yield multiple modules that match the pattern and
        base class. """

        if base is None:
            base = pwncat.modules.BaseModule

        for name, module in self.manager.modules.items():
            if (
                module.PLATFORM is not None
                and type(self.platform) not in module.PLATFORM
            ):
                continue
            if fnmatch.fnmatch(name, pattern) and isinstance(module, base):
                yield module

    def log(self, *args, **kwargs):
        """ Log to the console. This utilizes the active sessions
        progress instance to log without messing up progress output
        from other sessions, if we aren't active. """

        self.manager.log(f"{self.platform}:", *args, **kwargs)

    @property
    @contextlib.contextmanager
    def db(self):
        """ Retrieve a database session

        I'm not sure if this is the best way to handle database sessions.

        """

        new_session = self._db_session is None

        try:
            if new_session:
                self._db_session = self.manager.create_db_session()
            yield self._db_session
        finally:
            if new_session and self._db_session is not None:
                session = self._db_session
                self._db_session = None
                session.close()

    @contextlib.contextmanager
    def task(self, *args, **kwargs):
        """ Get a new task in this session's progress instance """

        # Ensure the variable exists even if an exception happens
        # prior to task creation
        task = None
        started = self._progress._started

        if "status" not in kwargs:
            kwargs["status"] = "..."

        try:
            # Ensure this bar is started if we are the selected
            # target.
            if self.manager.target == self:
                self._progress.start()
            # Create the new task
            task = self._progress.add_task(*args, **kwargs)
            yield task
        finally:
            if task is not None:
                # Delete the task
                self._progress.remove_task(task)
            # If the progress wasn't started when we entered,
            # ensure it is stopped before we leave. This allows
            # nested tasks.
            if not started:
                self._progress.stop()

    def update_task(self, task, *args, **kwargs):
        """ Update an active task """

        self._progress.update(task, *args, **kwargs)


class Manager:
    """
    ``pwncat`` manager which is responsible for creating channels,
    and sessions, managing the database sessions. It provides the
    factory functions for generating platforms, channels, database
    sessions, and executing modules.
    """

    def __init__(self, config: str = "./pwncatrc"):
        self.config = Config()
        self.sessions: List[Session] = []
        self.modules: Dict[str, pwncat.modules.BaseModule] = {}
        self.engine = None
        self.SessionBuilder = None
        self._target = None
        self.parser = CommandParser(self)
        self.interactive_running = False

        # Load standard modules
        self.load_modules(*pwncat.modules.__path__)

        # Get our data directory
        data_home = os.environ.get("XDG_DATA_HOME", "~/.local/share")
        if not data_home:
            data_home = "~/.local/share"

        # Expand the user path
        data_home = os.path.expanduser(os.path.join(data_home, "pwncat"))

        # Find modules directory
        modules_dir = os.path.join(data_home, "modules")

        # Load local modules if they exist
        if os.path.isdir(modules_dir):
            self.load_modules(modules_dir)

        # Load global configuration script, if available
        try:
            with open("/etc/pwncat/pwncatrc") as filp:
                self.parser.eval(filp.read(), "/etc/pwncat/pwncatrc")
        except (FileNotFoundError, PermissionError):
            pass

        # Load user configuration script
        user_rc = os.path.join(data_home, "pwncatrc")
        try:
            with open(user_rc) as filp:
                self.parser.eval(filp.read(), user_rc)
        except (FileNotFoundError, PermissionError):
            pass

        # Load local configuration script
        try:
            with open(config) as filp:
                self.parser.eval(filp.read(), config)
        except (FileNotFoundError, PermissionError):
            pass

    def open_database(self):
        """ Create the internal engine and session builder
        for this manager based on the configured database """

        if self.sessions and self.engine is not None:
            raise RuntimeError("cannot change database after sessions are established")

        self.engine = create_engine(self.config["db"])
        pwncat.db.Base.metadata.create_all(self.engine)
        self.SessionBuilder = sessionmaker(bind=self.engine)
        self.parser = CommandParser(self)

    def create_db_session(self):
        """ Create a new SQLAlchemy database session and return it """

        # Initialize a fallback database if needed
        if self.engine is None:
            self.config.set("db", "sqlite:///:memory:", glob=True)
            self.open_database()

        return self.SessionBuilder()

    @contextlib.contextmanager
    def new_db_session(self):
        """ Track a database session in a context manager """

        session = None

        try:
            session = self.create_db_session()
            yield session
        finally:
            pass

    def load_modules(self, *paths):
        """ Dynamically load modules from the specified paths

        If a module has the same name as an already loaded module, it will
        take it's place in the module list. This includes built-in modules.
        """

        for loader, module_name, _ in pkgutil.walk_packages(
            paths, prefix="pwncat.modules."
        ):
            module = loader.find_module(module_name).load_module(module_name)

            if getattr(module, "Module", None) is None:
                continue

            # Create an instance of this module
            module_name = module_name.split("pwncat.modules.")[1]
            self.modules[module_name] = module.Module()

            # Store it's name so we know it later
            setattr(self.modules[module_name], "name", module_name)

    def log(self, *args, **kwargs):
        """ Output a log entry """

        if self.target is not None:
            self.target._progress.log(*args, **kwargs)
        else:
            console.log(*args, **kwargs)

    @property
    def target(self) -> Session:
        """ Retrieve the currently focused target """
        return self._target

    @target.setter
    def target(self, value: Session):
        if value not in self.sessions:
            raise ValueError("invalid target")
        self._target = value

    def interactive(self):
        """ Start interactive prompt """

        self.interactive_running = True

        # This is required to ensure multi-byte key-sequences are read
        # properly
        old_stdin = sys.stdin
        sys.stdin = TextIOWrapper(
            os.fdopen(sys.stdin.fileno(), "br", buffering=0),
            write_through=True,
            line_buffering=False,
        )

        while self.interactive_running:

            # This is it's own main loop that will continue until
            # it catches a C-d sequence.
            try:
                self.parser.run()
            except InteractiveExit:

                if self.sessions and not confirm(
                    "There are active sessions. Are you sure?"
                ):
                    continue

                self.log("closing interactive prompt")
                break

            # We can't enter raw mode without a session
            if self.target is None:
                self.log("no active session, returning to local prompt")
                continue

            # NOTE - I don't like the selectors solution for async stream IO
            # Currently, we utilize the built-in selectors module.
            # This module depends on the epoll/select interface on Linux.
            # This requires that the challels are file-objects (have a fileno method)
            # I don't like this. I may switch to an asyncio-based wrapper in
            # the future, to alleviate requirements on channel implementations
            # but I'm not sure how to implement it right now.
            selector = selectors.DefaultSelector()
            selector.register(sys.stdin, selectors.EVENT_READ, None)
            selector.register(self.target.platform.channel, selectors.EVENT_READ, None)

            # Make the local terminal enter a raw state for
            # direct interaction with the remote shell
            term_state = pwncat.util.enter_raw_mode()

            self.target.platform.interactive = True

            try:
                # We do this until the user pressed <prefix>+C-d or
                # until the connection dies. Afterwards, we go back to
                # a local prompt.
                done = False
                has_prefix = False
                while not done:
                    for k, _ in selector.select():
                        if k.fileobj is sys.stdin:
                            data = sys.stdin.buffer.read(64)
                            has_prefix = self._process_input(
                                data, has_prefix, term_state
                            )
                        else:
                            data = self.target.platform.channel.recv(4096)
                            if data is None or len(data) == 0:
                                done = True
                                break
                            sys.stdout.buffer.write(data)
            except RawModeExit:
                pwncat.util.restore_terminal(term_state)
            except ChannelClosed:
                pwncat.util.restore_terminal(term_state)
                self.log(f"[yellow]warning[/yellow]: {self.target}: connection reset")
                self.target.died()
            except Exception:
                pwncat.util.restore_terminal(term_state)
                pwncat.util.console.print_exception()

            if self.target is not None:
                self.target.platform.interactive = False

    def create_session(self, platform: str, channel: Channel = None, **kwargs):
        """
        Open a new session from a new or existing platform. If the platform
        is a string, a new platform is created using ``create_platform`` and
        a session is built around the platform. In that case, the arguments
        are the same as for ``create_platform``.

        A new Session object is returned which contains the created or
        specified platform.
        """

        session = Session(self, platform, channel, **kwargs)
        return session

    def _process_input(self, data: bytes, has_prefix: bool, term_state):
        """ Process stdin data from the user in raw mode """

        for byte in data:
            byte = bytes([byte])

            if has_prefix:
                # Reset prefix flag
                has_prefix = False

                if byte == self.config["prefix"].value:
                    self.target.platform.channel.send(byte)
                else:
                    try:
                        binding = self.config.binding(byte)
                    except KeyError:
                        continue

                    if binding.strip().startswith("pass"):
                        self.target.platform.channel.send(byte)
                        binding = binding.lstrip("pass")
                    else:
                        pwncat.util.restore_terminal(term_state)
                        sys.stdout.write("\n")

                        self.parser.eval(binding, "<binding>")

                        self.target.platform.channel.send(b"\n")
                        pwncat.util.enter_raw_mode()
            elif byte == self.config["prefix"].value:
                has_prefix = True
            elif data == pwncat.config.KeyType("c-d").value:
                raise RawModeExit
            else:
                self.target.platform.channel.send(byte)

        return has_prefix