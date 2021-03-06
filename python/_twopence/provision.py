##################################################################
#
# Handle the actual provisioning.
#
# Right now, this is tailored to the vagrant backend, and may
# need to be enhanced for other backends.
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import os
import twopence

from .logging import *

class ProvisioningFile:
	def __init__(self, *names):
		self.name = os.path.join(*names)
		self.path = os.path.join(twopence.provision_script_dir, *names)

	def load(self):
		path = self.path

		debug(f"Trying to load script snippet from {path}")

		result = ["", f"# BEGIN {path}"]
		with open(path, "r") as f:
			result += f.read().split('\n')
		result.append(f"# END OF {path}")

		return result

class ProvisioningScriptSnippet:
	def __init__(self, name, reboot, lines = []):
		self.name = name
		self.reboot = reboot
		self.script = lines + []
		self.functionsLoaded = set()

	def __str__(self):
		return "%s(%s, %u lines)" % (self.__class__.__name__, self.name, len(self.script))

	def appendCommand(self, cmd):
		self.script.append(cmd)

	def merge(self, stage):
		for file in stage.files():
			if file.path not in self.functionsLoaded:
				self.script += file.load()
				self.functionsLoaded.add(file.path)

		self.script += stage.shellCommands()

	def format(self, indent = ""):
		result = ""
		for line in self.script:
			if line:
				result += indent + line
			result += '\n'
		return result

	@property
	def empty(self):
		return not(self.script)

class ProvisioningScriptCollection:
	def __init__(self, stages, env, preamble = None):
		assert(isinstance(env, ProvisioningShellEnvironment))
		self.scripts = []
		self._variables = env
		self._preamble = preamble

		script = self.createScript("default")

		for stage in stages:
			# print("build script for %s" % stage)
			if stage.reboot:
				script = self.createScript(stage.name, reboot = stage.reboot)

			debug("Processing stage %s -> %s" % (stage.name, script.name))
			script.merge(stage)

	def __iter__(self):
		return iter(self.scripts)

	def createScript(self, name, reboot = False):
		script = ProvisioningScriptSnippet(name, reboot, self._variables._env + self._preamble)
		self.scripts.append(script)
		# script.appendCommand("set -x")
		return script

class ProvisioningShellEnvironment:
	def __init__(self):
		self._env = []

	def export(self, name, value):
		debug("  %s='%s'" % (name, value))

		if value is None or value == []:
			self._env.append("%s=''" % (name))
		elif type(value) in (list, set, tuple):
			self._env.append("%s='%s'" % (name, " ".join(value)))
		elif type(value) in (str, bool, int):
			self._env.append("%s='%s'" % (name, value))
		else:
			raise NotImplementedError("shell variable assignment %s=%s" % (name, value))

	def __iter__(self):
		return iter(self._env)

class Provisioner:
	def processTemplate(self, nodeConfig, templatePath, outputPath, extraData = None):
		if not templatePath.startswith('/'):
			templatePath = os.path.join("/usr/lib/twopence/provision", templatePath)

		print("Creating %s from %s" % (outputPath, templatePath))

		data = self.nodeConfigAsDict(nodeConfig, extraData)

		tmpf = open(templatePath, "r")
		outf = open(outputPath, "w")

		lineNumber = 0
		for line in tmpf.readlines():
			lineNumber += 1

			output = ""
			while '@' in line:
				i = line.index('@')
				output += line[:i]

				line = line[i+1:]
				i = line.find('@')
				if i < 0:
					raise ValueError("lone @ in %s:%d" % (templatePath, lineNumber))

				if i == 0:
					# @@ is written out as @
					output += '@'
				else:
					key = line[:i]

					value = data.get(key)
					if value is None:
						raise ValueError("%s:%s: unknown key \"%s\"" % (templatePath, lineNumber, key))

					if type(value) == set:
						value = list(value)

					if type(value) in (list, tuple):
						if len(value) == 0:
							debug("%s:%s: key %s expands to empty list" % (templatePath, lineNumber, key))
							value = ""
						else:
							for l in value[:-1]:
								outf.write(output + l + line[i+1:])
							value = value[-1]

					output += value

				line = line[i+1:]

			output += line
			outf.write(output)

	def nodeConfigAsDict(self, nodeConfig, extraData, list_sepa = " "):
		d = {}

		d['FEATURES'] = nodeConfig.features
		d['REQUIRES'] = nodeConfig.requires

		if extraData is not None:
			for name, value in extraData.items():
				existing = d.get(name)
				if type(existing) == list:
					existing += value
				else:
					d[name] = value

		return d
