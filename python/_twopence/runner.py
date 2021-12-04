##################################################################
#
# Shell command runner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import subprocess
import time
from .util import ProgressBar

class CommandTimeout(Exception):
	pass

class Alarm:
	active = None

	def __init__(self, timeout):
		from signal import alarm, signal, SIGALRM, SIG_DFL

		self._alarm = alarm
		signal(SIGALRM, self.alarm_handler)

		if timeout:
			self.setAlarm(timeout)

	def __del__(self):
		self.cancel()

	@staticmethod
	def alarm_handler(*args):
		Alarm.active = None
		raise CommandTimeout()

	def setAlarm(self, timeout):
		assert(Alarm.active is None)
		Alarm.active = self

		self._alarm(int(timeout))

	def cancel(self):
		if Alarm.active == self:
			self._alarm(0)
			Alarm.active = None

class ExecStatus:
	RUNNING = 0
	TIMED_OUT = 1
	EXITED = 2
	CRASHED = 3

	def __init__(self, how, exit_code = None, output = ""):
		self.how = how

		if how != self.EXITED:
			exit_code = None
		self.exit_code = exit_code

		if type(output) == str:
			self.output = output.split("\n")
		else:
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

class Runner:
	def run(self, command, cwd = None, timeout = 10, quiet = False):
		if cwd:
			print("Executing \"%s\" in directory %s" % (command, cwd))
		else:
			print("Executing \"%s\"" % command)

		p = subprocess.Popen(command,
				cwd = cwd,
				encoding = "utf8",
				stdout = subprocess.PIPE,
				stderr = subprocess.STDOUT,
				shell = True,
				bufsize = 0)

		startTime = time.time()
		alarm = Alarm(timeout)
		output = []

		while p.poll() is None:
			try:
				l = p.stdout.readline().strip()
				if not quiet:
					if len(output) == 0:
						print("Command output:")

					print("[%s] %s" % (self.formatTimestamp(startTime), l))
				output.append(l)
			except CommandTimeout:
				print("[%s] Command Timed Out." % (self.formatTimestamp(startTime), ))
				p.kill()
				return ExecStatus.timedOut(output)

		alarm.cancel()

		return ExecStatus.exited(p.returncode, output)

	def formatTimestamp(self, since):
		elapsed = time.time() - since
		minutes = int(elapsed / 60)
		seconds = elapsed - minutes * 60
		return "%02u:%05.2f" % (minutes, seconds)

	def runOld(self, command, cwd = None, timeout = 10, quiet = False):
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


