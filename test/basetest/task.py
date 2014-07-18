# -*- coding: utf-8 -*-

import os
import tempfile
import shutil
import atexit
import unittest
from .utils import run_cmd_wait, run_cmd_wait_nofail, which
from .exceptions import CommandError


class Task(object):
    """Manage a task warrior instance

    A temporary folder is used as data store of task warrior.
    This class can be instanciated multiple times if multiple taskw clients are
    needed.

    This class can be given a Taskd instance for simplified configuration.

    A taskw client should not be used after being destroyed.
    """
    def __init__(self, taskw="task", taskd=None):
        """Initialize a Task warrior (client) that can interact with a taskd
        server. The task client runs in a temporary folder.

        :arg taskw: Task binary to use as client (defaults: task in PATH)
        :arg taskd: Taskd instance for client-server configuration
        """
        self.taskw = taskw
        self.taskd = taskd

        # Used to specify what command to launch (and to inject faketime)
        self._command = [self.taskw]

        # Configuration of the isolated environment
        self._original_pwd = os.getcwd()
        self.datadir = tempfile.mkdtemp(prefix="task_")
        self.taskrc = os.path.join(self.datadir, "test.rc")

        # Ensure any instance is properly destroyed at session end
        atexit.register(lambda: self.destroy())

        self.reset_env()

        # Cannot call self.config until confirmation is disabled
        with open(self.taskrc, 'w') as rc:
            rc.write("data.location={0}\n"
                     "confirmation=no\n".format(self.datadir))

        # Setup configuration to talk to taskd automatically
        if self.taskd is not None:
            self.bind_taskd_server(self.taskd)

    def __repr__(self):
        txt = super(Task, self).__repr__()
        return "{0} running from {1}>".format(txt[:-1], self.datadir)

    def reset_env(self):
        """Set a new environment derived from the one used to launch the test
        """
        # Copy all env variables to avoid clashing subprocess environments
        self.env = os.environ.copy()

        # Make sure no TASKDDATA is isolated
        self.env["TASKDATA"] = self.datadir
        # As well as TASKRC
        self.env["TASKRC"] = self.taskrc

    def __call__(self, *args, **kwargs):
        "aka t = Task() ; t() which is now an alias to t.runSuccess()"
        return self.runSuccess(*args, **kwargs)

    def bind_taskd_server(self, taskd):
        """Configure the present task client to talk to given taskd server

        Note that this can be performed automatically by passing taskd when
        creating an instance of the current class.
        """
        self.taskd = taskd

        cert = os.path.join(self.taskd.certpath, "test_client.cert.pem")
        key = os.path.join(self.taskd.certpath, "test_client.key.pem")
        self.config("taskd.certificate", cert)
        self.config("taskd.key", key)
        self.config("taskd.ca", self.taskd.ca_cert)

        address = ":".join((self.taskd.address, str(self.taskd.port)))
        self.config("taskd.server", address)

        # Also configure the default user for given taskd server
        self.set_taskd_user()

    def set_taskd_user(self, taskd_user=None, default=True):
        """Assign a new user user to the present task client

        If default==False, a new user will be assigned instead of reusing the
        default taskd user for the corresponding instance.
        """
        if taskd_user is None:
            if default:
                user, group, org, userkey = self.taskd.default_user
            else:
                user, group, org, userkey = self.taskd.create_user()
        else:
            user, group, org, userkey = taskd_user

        self.credentials = "/".join((org, user, userkey))
        self.config("taskd.credentials", self.credentials)

    def config(self, var, value):
        """Run setup `var` as `value` in taskd config
        """
        # Add -- to avoid misinterpretation of - in things like UUIDs
        cmd = (self.taskw, "config", "--", var, value)
        return run_cmd_wait(cmd, env=self.env)

    def runSuccess(self, args=(), input=None, merge_streams=True):
        """Invoke task with the given arguments

        Use runError if you want exit_code to be tested automatically and
        *not* fail if program finishes abnormally.

        If you wish to pass instructions to task such as confirmations or other
        input via stdin, you can do so by providing a input string.
        Such as input="y\ny".

        If merge_streams=True stdout and stderr will be merged into stdout.

        Returns (exit_code, stdout, stderr)
        """
        # Create a copy of the command
        command = self._command[:]
        command.extend(args)

        output = run_cmd_wait_nofail(command, input,
                                     merge_streams=merge_streams, env=self.env)

        if output[0] != 0:
            raise CommandError(command, *output)

        return output

    def runError(self, args=(), input=None, merge_streams=True):
        """Same as runSuccess but Invoke task with the given arguments

        Use runSuccess if you want exit_code to be tested automatically and
        *fail* if program finishes abnormally.

        If you wish to pass instructions to task such as confirmations or other
        input via stdin, you can do so by providing a input string.
        Such as input="y\ny".

        If merge_streams=True stdout and stderr will be merged into stdout.

        Returns (exit_code, stdout, stderr)
        """
        # Create a copy of the command
        command = self._command[:]
        command.extend(args)

        output = run_cmd_wait_nofail(command, input,
                                     merge_streams=merge_streams, env=self.env)

        # output[0] is the exit code
        if output[0] == 0 or output[0] is None:
            raise CommandError(command, *output)

        return output

    def destroy(self):
        """Cleanup the data folder and release server port for other instances
        """
        try:
            shutil.rmtree(self.datadir)
        except OSError as e:
            if e.errno == 2:
                # Directory no longer exists
                pass
            else:
                raise

        # Prevent future reuse of this instance
        self.runSuccess = self.__destroyed
        self.runError = self.__destroyed

        # self.destroy will get called when the python session closes.
        # If self.destroy was already called, turn the action into a noop
        self.destroy = lambda: None

    def __destroyed(self, *args, **kwargs):
        raise AttributeError("Task instance has been destroyed. "
                             "Create a new instance if you need a new client.")

    def diag(self, merge_streams_with=None):
        """Run task diagnostics.

        This function may fail in which case the exception text is returned as
        stderr or appended to stderr if merge_streams_with is set.

        If set, merge_streams_with should have the format:
        (exitcode, out, err)
        which should be the output of any previous process that failed.
        """
        try:
            output = self.runSuccess(("diag",))
        except CommandError as e:
            # If task diag failed add the error to stderr
            output = (e.code, None, str(e))

        if merge_streams_with is None:
            return output
        else:
            # Merge any given stdout and stderr with that of "task diag"
            code, out, err = merge_streams_with
            dcode, dout, derr = output

            # Merge stdout
            newout = "\n##### Debugging information (task diag): #####\n{0}"
            if dout is None:
                newout = newout.format("Not available, check STDERR")
            else:
                newout = newout.format(dout)

            if out is not None:
                newout = out + newout

            # And merge stderr
            newerr = "\n##### Debugging information (task diag): #####\n{0}"
            if derr is None:
                newerr = newerr.format("Not available, check STDOUT")
            else:
                newerr = newerr.format(derr)

            if err is not None:
                newerr = err + derr

            return code, newout, newerr

    def faketime(self, faketime=None):
        """Set a faketime using libfaketime that will affect the following
        command calls.

        If faketime is None, faketime settings will be disabled.
        """
        cmd = which("faketime")
        if cmd is None:
            raise unittest.SkipTest("libfaketime/faketime is not installed")

        if self._command[0] == cmd:
            self._command = self._command[3:]

        if faketime is not None:
            # Use advanced time format
            self._command = [cmd, "-f", faketime] + self._command

# vim: ai sts=4 et sw=4