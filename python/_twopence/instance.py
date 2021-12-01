##################################################################
#
# Node abstraction for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import time
import os

class Network:
	AF_IPv4 = 4
	AF_IPv6 = 6

	VALID_FAMILIES = (AF_IPv4, AF_IPv6)

class NetworkInterface:
	def __init__(self, family, address = None, prefix_len = None):
		assert(family in Network.VALID_FAMILIES)

		if prefix_len is None:
			if family == Network.AF_IPv4:
				prefix_len = 24
			elif family == Network.AF_IPv6:
				prefix_len = 64
			else:
				raise ValueError("not default prefix len for AF %s" % af)

		self.family = family
		self.address = address
		self.prefix_len = prefix_len
		self.network = "%s/%s" % (address, prefix_len)

	def __str__(self):
		return "%s/%s" % (self.address, self.prefix_len)

##################################################################
# Generic functionality for node instances (eg VMs)
# Backends derive from this base class
##################################################################
class GenericInstance:
	def __init__(self, instanceConfig, workspace, persistentState = None):
		self.config = instanceConfig
		self.workspace = workspace
		self.persistent = persistentState
		self.name = instanceConfig.name

		self.exists = False

		self.running = False
		self.networkInterfaces = []

		if self.persistent:
			instanceConfig.persistInfo(self.persistent)

	def addNetworkInterface(self, af, address, prefix_len = None):
		af = int(af)

		nif = NetworkInterface(af, address, prefix_len)
		self.networkInterfaces.append(nif)

		# Reflect the first address of this family in the status file
		self.persistNetwork()

	def clearNetworkInterfaces(self):
		self.networkInterfaces = []
		self.persistNetwork()

	def fetchNeworksFromPersistentState(self):
		if self.persistent is None:
			return

		self.networkInterfaces = []

		address = self.persistent.ipv4_address
		if address:
			self.addNetworkInterface(Network.AF_IPv4, address)
		address = self.persistent.ipv6_address
		if address:
			self.addNetworkInterface(Network.AF_IPv6, address)

	def persistNetwork(self):
		if self.persistent is None:
			return

		self.persistent.ipv4_address = self.getFirstNetworkAddress(Network.AF_IPv4)
		self.persistent.ipv6_address = self.getFirstNetworkAddress(Network.AF_IPv6)

	def getFirstNetworkAddress(self, af):
		for nif in self.networkInterfaces:
			if nif.family == af:
				return nif.address
		return None

	def recordStartTime(self, when):
		if self.persistent is None:
			return

		self.persistent.set_value("start-time", when)

	def recordKeyfile(self, path):
		if self.persistent is None:
			return

		self.persistent.set_value("keyfile", path)

	def recordTarget(self, target):
		if self.persistent is None:
			return

		self.persistent.set_value("target", target)

	def saveLog(self, filename, buffer):
		with self.openLog(filename) as f:
			f.write(buffer)

	def saveExecStatus(self, filename, status):
		with self.openLog(filename) as f:
			print("%s %s" % (time.ctime(), status), file = f)
			if status.output:
				print("Command output follows", file = f)
				f.write(status.output)

	def openLog(self, filename):
		path = os.path.join(self.workspace, filename)
		return open(path, "w")

class InstanceConfig:
	def __init__(self, name, status = None):
		self.name = name
		self.platform = None
		self.image = None
		self.keyfile = None
		self.repositories = []
		self.install = []
		self.start = []
		self.features = []

	def enableRepositories(self, repo_names):
		for name in repo_names:
			repo = self.platform.getRepository(name)
			if repo is None:
				raise ValueError("instance %s wants to use repository %s, but platform %s does not define it" % (
							self.name, name, self.platform.name))

			if repo not in self.repositories:
				self.repositories.append(repo)

	def installPackages(self, package_list):
		for name in package_list:
			if name not in self.install:
				self.install.append(name)

	def startServices(self, service_list):
		for name in service_list:
			if name not in self.start:
				self.start.append(name)

	def enableFeatures(self, name_list):
		self.features += name_list

	def persistInfo(self, nodePersist):
		nodePersist.features = self.features
		if self.platform:
			nodePersist.vendor = self.platform.vendor
			nodePersist.os = self.platform.os

	def processTemplate(self, templatePath, outputPath, extraCommands = []):
		print("Creating %s from %s" % (outputPath, templatePath))

		data = self.asDict()

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

	def asDict(self, list_sepa = " "):
		d = {}

		d['NAME'] = self.name
		d['HOSTNAME'] = self.name
		d['PLATFORM'] = self.platform.name
		d['IMAGE'] = self.image or ""
		d['KEYFILE'] = self.keyfile or ""
		d['REPOSITORIES'] = list_sepa.join(repo.url for repo in self.repositories)
		d['INSTALL'] = list_sepa.join(self.install)
		d['START'] = list_sepa.join(self.start)

		# FIXME: should we manually install the package signing keys?
		# We could download them from $url/repodata/repomd.xml.key
		# and have the config refer to the file...

		cmdlist = []
		for repo in self.repositories:
			if repo.keyfile:
				# FIXME: upload the keyfile to the backend, and issue an
				# "rpm --import keyfile" command
				keyfile = repo.keyfile
			else:
				keyfile = "%s/repodata/repomd.xml.key" % repo.url

			if keyfile.startswith("http:") or keyfile.startswith("https:"):
				cmdlist.append("rpm --import " + keyfile)
			else:
				raise NotImplementedError("Cannot upload keyfile to instance")

			cmdlist.append("zypper ar %s %s" % (repo.url, repo.name))
		d['ADD_REPOSITORIES'] = cmdlist

		if self.install:
			d['INSTALL_PACKAGES'] = "zypper in -y " + " ".join(self.install)
		else:
			d['INSTALL_PACKAGES'] = ""

		d['COMMANDS'] = []

		return d
