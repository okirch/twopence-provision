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
from .config import ConfigError
from .logging import *

class Packager:
	@classmethod
	def forPlatform(klass, vendor, os):
		if vendor == "suse":
			return Zypper()

		raise NotImplementedError("No packager for %s/%s" % (vendor, os))

	def importGpgKey(self, keyfile):
		raise NotImplementedError()

	def enableRepository(self, url, name):
		raise NotImplementedError()

	def installPackages(self, pkg_list):
		raise NotImplementedError()

class Zypper(Packager):
	def importGpgKey(self, keyfile):
		return "rpm --import " + keyfile

	def enableRepository(self, url, name):
		return "zypper ar %s %s" % (url, name)

	def installPackages(self, pkg_list):
		return "zypper in -y " + " ".join(pkg_list)

class Provisioner:
	def processTemplate(self, nodeConfig, templatePath, outputPath, extraCommands = []):
		if not templatePath.startswith('/'):
			templatePath = os.path.join("/usr/lib/twopence/provision", templatePath)

		print("Creating %s from %s" % (outputPath, templatePath))

		data = self.nodeConfigAsDict(nodeConfig)

		data['COMMANDS'] += extraCommands

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

					if type(value) == list:
						if len(value) == 0:
							debug("%s:%s: key %s expands to empty list" % (templatePath, lineNumber, key))
							value = ""
						else:
							for l in value[:-1]:
								outf.write(output + l + "\n")
							value = value[-1]

					output += value

				line = line[i+1:]

			output += line
			outf.write(output)

	def nodeConfigAsDict(self, nodeConfig, list_sepa = " "):
		packager = Packager.forPlatform(nodeConfig.vendor, nodeConfig.os)

		d = {}

		d['NAME'] = nodeConfig.name
		d['HOSTNAME'] = nodeConfig.name
		d['PLATFORM'] = nodeConfig.platform.name
		d['VENDOR'] = nodeConfig.platform.vendor
		d['OS'] = nodeConfig.platform.os
		d['ARCH'] = nodeConfig.platform.arch
		d['IMAGE'] = nodeConfig.image or ""
		d['KEYFILE'] = nodeConfig.keyfile or ""
		d['REPOSITORIES'] = list_sepa.join(repo.url for repo in nodeConfig.repositories)
		d['INSTALL'] = list_sepa.join(nodeConfig.install)
		d['START'] = list_sepa.join(nodeConfig.start)
		d['FEATURES'] = nodeConfig.features
		d['REQUIRES'] = nodeConfig.requires

		# FIXME: should we manually install the package signing keys?
		# We could download them from $url/repodata/repomd.xml.key
		# and have the config refer to the file...

		cmdlist = []
		for repo in nodeConfig.repositories:
			if repo.keyfile:
				# FIXME: upload the keyfile to the backend, and issue an
				# "rpm --import keyfile" command
				keyfile = repo.keyfile
			else:
				keyfile = "%s/repodata/repomd.xml.key" % repo.url

			if keyfile.startswith("http:") or keyfile.startswith("https:"):
				cmdlist.append(packager.importGpgKey(keyfile))
			else:
				raise NotImplementedError("Cannot upload keyfile to instance")

			cmdlist.append(packager.enableRepository(repo.url, repo.name))

		d['ADD_REPOSITORIES'] = cmdlist

		if nodeConfig.install:
			d['INSTALL_PACKAGES'] = packager.installPackages(nodeConfig.install)
		else:
			d['INSTALL_PACKAGES'] = ""

		d['COMMANDS'] = []

		self.extraInfoToDict(d, nodeConfig.info, "info")
		self.extraInfoToDict(d, nodeConfig.platform.info, "platform_info")

		return d

	def extraInfoToDict(self, d, info, prefix):
		if not info:
			return
		for key, value in info.items():
			key = "%s_%s" % (prefix, key)
			d[key.upper()] = value
