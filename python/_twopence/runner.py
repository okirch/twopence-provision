##################################################################
#
# Shell command runner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################


from .util import ProgressBar

class ExecStatus:
	RUNNING = 0
	TIMED_OUT = 1
	EXITED = 2
	CRASHED = 3

	def __init__(self, how, exit_code = None, output = None):
		self.how = how

		if how != self.EXITED:
			exit_code = None
		self.exit_code = exit_code

		self.output = output

	@classmethod
	def timedOut(klass, *args, **kwargs):
		return klass(klass.TIMED_OUT, *args, **kwargs)

	@classmethod
	def exited(klass, *args, **kwargs):
		return klass(klass.EXITED, *args, **kwargs)

	def __bool__(self):
		return self.exit_code == 0

	def __str__(self):
		if self.how == self.RUNNING:
			return "running"
		if self.how == self.TIMED_OUT:
			return "timed out"
		if self.how == self.EXITED:
			return "exited with code %d" % self.exit_code
		if self.how == self.CRASHED:
			return "crashed"

		return "UNKNOWN"

	@property
	def output_lines(self):
		if self.output is None:
			return []
		return self.output.split("\n")


class Runner:
	def run(self, command, cwd = None, timeout = 10, quiet = False):
		import subprocess
		import time

		progress = ProgressBar("Waiting for command to complete")
		if quiet:
			progress.disable()
		elif cwd:
			print("Executing \"%s\" in directory %s" % (command, cwd))
		else:
			print("Executing \"%s\"" % command)

		p = subprocess.Popen(command,
				cwd = cwd,
				encoding = "utf8",
				stdout = subprocess.PIPE,
				stderr = subprocess.STDOUT,
				shell = True)

		endTime = time.time() + timeout
		delay = 2

		while p.returncode is None:
			if time.time() > endTime:
				progress.finish("timed out.")
				p.kill()
				output, error = p.communicate()
				return ExecStatus.timedOut(str(output) + str(error))
			try:
				p.wait(delay)
			except subprocess.TimeoutExpired as e:
				progress.tick()

			delay = 0.5

		progress.finish("done.")

		return ExecStatus.exited(p.returncode, p.stdout.read())


