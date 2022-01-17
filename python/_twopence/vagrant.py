##################################################################
#
# Vagrant backend for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################
import os
import json
import shutil
import copy
import time

from .logging import *
from .backend import Backend
from .runner import Runner
from .instance import *
from .provision import *
from .config import Config, Configurable, ConfigError
from .util import DottedNumericVersion

VagrantRebootBlock = '''
  config.vm.provision :shell do |shell|
    shell.privileged = true
    shell.inline = 'echo rebooting'
    shell.reboot = true
  end
'''

VagrantShellHeader = '''
  config.vm.provision "shell", inline: <<-SHELL
'''
VagrantShellTrailer = '''
  SHELL
'''

class VagrantBoxInfo:
	ORIGIN_LOCAL = "local"
	ORIGIN_VAGRANTCLOUD = "vagrant"

	def __init__(self, name = None, version = None, provider = None, url = None, origin = None):
		self.name = name
		self._version = DottedNumericVersion(version)
		self.provider = provider
		self.url = url
		self.origin = origin or self.ORIGIN_LOCAL

	@property
	def version(self):
		return str(self._version)

	@version.setter
	def version(self, value):
		self._version = DottedNumericVersion(value)

	def __str__(self):
		return "vagrant image %s/%s (origin %s)" % (self.name, self.version, self.origin)

	# practically the same, except for the version
	def similar(self, other):
		if not isinstance(other, self.__class__):
			return False

		if self.name != other.name:
			return False

		if self.origin == other.origin:
			return True

		if self.origin == self.ORIGIN_VAGRANTCLOUD or \
		   other.origin == self.ORIGIN_VAGRANTCLOUD:
			return True

		return False

	def __eq__(self, other):
		if not self.similar(other):
			return False
		return self._version == other._version

	def __lt__(self, other):
		if not self.similar(other):
			return False
		return self._version < other._version

	def __le__(self, other):
		if not self.similar(other):
			return False
		return self._version <= other._version

class VagrantBoxMeta:
	def __init__(self, name, url = None, provider = "libvirt"):
		self.name = name
		self.base_url = url
		self.origin = None
		self.provider = provider
		self.downloadUrl = None
		self.boxes = []

		if url.startswith("vagrant:"):
			self.origin = VagrantBoxInfo.ORIGIN_VAGRANTCLOUD

			remote = url[8:]
			if remote != name:
				raise ConfigError("Error in definition of vagrant image %s: image name must equal remote name %s" % (
							name, remote))

			debug("URL %s refers to image on vagrantcloud" % url)
			data = self.tryVagrantCloud(remote)
			if data is None:
				verbose("unable to retrieve image from \"%s\" - faking it" % url)

			self.downloadUrl = remote
		elif url and url.startswith("/"):
			self.origin = VagrantBoxInfo.ORIGIN_LOCAL

			debug("URL %s refers to local image" % url)
			data = self.tryLocal(url)
			if data is None:
				return None

			self.downloadUrl = url
		else:
			raise ConfigError("vagrant: don't know how to handle image url %s" % url)

		if name is None:
			name = data.get('name')
			if not name:
				raise ConfigError("Image at %s does not provide a name" % url)
			self.name = name

		for version in data.get('versions') or []:
			for actual_version in version.get('providers') or []:
				self.addBox(version = version.get('version'),
						provider = actual_version.get('name'),
						url = actual_version.get('url')
						)

	@staticmethod
	def load(name, url):
		debug("VagrantBoxMeta.load(%s, %s)" % (name, url))
		return VagrantBoxMeta(name, url)

	def addBox(self, **kwargs):
		box = VagrantBoxInfo(self.name, **kwargs, origin = self.origin)
		self.boxes.append(box)
		return box

	def getLatestVersion(self, provider = "libvirt"):
		best = None
		for box in self.boxes:
			if box.provider != provider:
				continue
			if best is None or best < box:
				best = box
		return best

	def getDownloadFor(self, box):
		if self.downloadUrl:
			box = copy.copy(box)
			box.url = self.downloadUrl
		return box

	def tryLocal(self, url):
		if not os.path.isfile(url):
			return None

		with open(url) as f:
			try:
				data = json.load(f)
			except:
				return None

		return data

	def tryVagrantCloud(self, name):
		import requests

		url = "https://vagrantcloud.com/%s" % name
		resp = requests.get(url)
		if not resp.ok:
			warning("Failed to download %s: %s" % (url, resp.reason))
			return None

		content_type = resp.headers['content-type'].split(';')[0]
		if content_type != 'application/json':
			warning("Bad content type from %s: %s" % (url, content_type))
			return None

		return resp.json()

class VagrantBoxListing:
	def __init__(self):
		self.boxes = []

	def create(self):
		box = VagrantBoxInfo()
		self.boxes.append(box)
		return box

	def find(self, name, provider = "libvirt", version = None):
		for box in self.boxes:
			if version is not None and box.version != version:
				continue
			if box.name == name and box.provider == provider:
				return box
		return None

	def __contains__(self, wanted):
		if wanted is None:
			return False
		for box in self.boxes:
			if box == wanted:
				return True
		return False


class VagrantInstance(GenericInstance):
	def setStateFromVagrantStatus(self, raw_status):
		# debug("setStateFromVagrantStatus(%s, raw=%s, persistent=%s)" % (self.name, raw_status, self.persistent))
		if raw_status in ('preparing', 'running', ):
			self.running = True

			self.fetchNeworksFromPersistentState()
		elif raw_status in ('not_started', 'shutoff', 'not_created'):
			self.running = False

			self.clearNetworkInterfaces()
		else:
			raise ValueError("Vagrant instance %s/default is in state %s - huh?!" % (
					self.name, raw_status))

		self.raw_state = raw_status

class VagrantNodeConfig(Configurable):
	info_attrs = ['template', 'image', 'url']

	def __init__(self):
		super().__init__()
		self.image = None
		self.url = None
		self.template = None
		self.timeout = 120

	def configure(self, config):
		self.update_value(config, "template")
		self.update_value(config, "image")
		self.update_value(config, "url")
		self.update_value(config, "timeout", typeconv = float)

class VagrantBackend(Backend):
	name = "vagrant"

	def __init__(self):
		debug("Created vagrant backend")
		super().__init__()

		self.template = None
		self.runner = Runner()
		self.provisioner = Provisioner()

		# the vagrant box listing
		self.listing = None

	def configure(self, config):
		self.update_value(config, 'template')

	def configureNode(self, node, config):
		node.vagrant = VagrantNodeConfig()

		# in case someone configured a global template
		node.template = self.template

		for config in node.platform.backends.savedConfigs("vagrant"):
			node.vagrant.configure(config)
		for config in node.backends.savedConfigs("vagrant"):
			node.vagrant.configure(config)

		debug("Vagrant configureNode(%s) = %s" % (node.name, node.vagrant))

	def detect(self, workspace, status, expectedInstanceConfigs):
		assert(workspace)

		found = []
		for instanceConfig in expectedInstanceConfigs:
			savedState = status.createNodeState(instanceConfig.name)

			instance = self.detectInstance(workspace, instanceConfig, savedState)
			if instance:
				found.append(instance)

		for savedInstanceState in status.nodes:
			if any(instance.name == savedInstanceState.name for instance in found):
				continue

			dummy = Config.createEmptyNode(savedInstanceState.name)
			instance = self.detectInstance(workspace, dummy, savedInstanceState)
			if instance:
				found.append(instance)

		return found

	def detectInstance(self, workspace, instanceConfig, savedInstanceState = None):
		assert(workspace)

		debug("detectInstance(%s)" % instanceConfig.name)
		instanceWorkspace = os.path.join(workspace, instanceConfig.name)

		magic_path = os.path.join(instanceWorkspace, ".vagrant")
		if not os.path.isdir(magic_path):
			return None

		debug("Instance %s: workspace exists" % instanceConfig.name)

		instance = VagrantInstance(instanceConfig, instanceWorkspace, savedInstanceState)
		instance.exists = True

		# This calls setStateFromVagrantStatus(), which will do one of these
		# - if a VM is running, instance.networkInterfaces is initialized from
		#   the node's persistent state
		# - if no VM is running, clear instance.networkInterfaces and update
		#   the node's persistent state (ie delete ipv4_address and friends)
		self.detectInstanceState(instance)

		debug("Detected instance %s (state %s)" % (instance.name, instance.raw_state))
		return instance

	def identifyImageToDownload(self, instanceConfig):
		vagrantNode = instanceConfig.vagrant

		known = self.listBoxes()

		# See if we have any version of that image
		have = known.find(vagrantNode.image, version = None)

		# If the image does not come with a .json meta file, check whether
		# we have an unversioned image of that name
		meta = VagrantBoxMeta.load(vagrantNode.image, vagrantNode.url)
		if meta is None:
			if have:
				debug("No need to download image %s; unversioned image already present" % (
						have.name))
				return None
		else:
			want = meta.getLatestVersion()

			if want in known:
				debug("No need to download %s; already present" % want)
				return None

			if not self.auto_update:
				if have is not None:
					debug("We have %s; latest version is %s (not updating)" % (have, want))
					return None

			if want:
				return meta.getDownloadFor(want)

		return VagrantBoxInfo(name = vagrantNode.image, url = vagrantNode.url, provider = "libvirt")

	def downloadImage(self, workspace, instanceConfig):
		download = self.identifyImageToDownload(instanceConfig)
		if download:
			self.addImage(download)

		return True

	def prepareInstance(self, workspace, instanceConfig, savedInstanceState):
		assert(workspace)

		template = instanceConfig.vagrant.template or self.template
		if template is None:
			raise ValueError("Cannot prepare vagrant instance - no template defined")

		instanceWorkspace = os.path.join(workspace, instanceConfig.name)

		# If the instance workspace exists already, we should fail.
		# However, it may be a leftover from an aborted attempt.
		# Try to be helpful and remove the workspace IFF it is empty
		if os.path.isdir(instanceWorkspace):
			try:	os.rmdir(instanceWorkspace)
			except: pass

		if os.path.isdir(instanceWorkspace):
			raise ValueError("workspace %s already exists" % instanceWorkspace)

		os.makedirs(instanceWorkspace)

		path = os.path.join(workspace, instanceConfig.name, "Vagrantfile")

		extraData = {}

		# instanceConfig gives us a list of scriptlets for provisioning.
		# We turn these into a set of config.vm.provision blocks for the
		# Vagrantfile
		provisioning = self.buildProvisioning(instanceConfig)
		extraData["VAGRANT_PROVISION_STAGES"] = "\n".join(provisioning)

		extraData["VAGRANT_MACHINE_CONFIG"] = self.buildMachineConfig(instanceConfig)

		vagrantNode = instanceConfig.vagrant

		# For the time being, we simply push the vagrant image name to the
		# provisioner by setting instanceConfig.image. Not very clean, but
		# everything else will turn too byzantine.
		instanceConfig.platform.image = vagrantNode.image
		debug("Using vagrant box %s" % instanceConfig.image)

		self.provisioner.processTemplate(instanceConfig, template, path, extraData)

		return VagrantInstance(instanceConfig, instanceWorkspace, savedInstanceState)

	def buildProvisioning(self, instanceConfig):
		# Add backend specific provisioning commands
		pstage = instanceConfig.createStage("provision")
		pstage.zap()

		# This tells the twopence server where to listen for incoming
		# connections.
		pstage.run.append("twopence-tcp")

		result = []
		for s in instanceConfig.cookedStages():
			formatted = ""

			if s.reboot:
				formatted += VagrantRebootBlock
			formatted += VagrantShellHeader
			formatted += s.format(indent = "    ")
			formatted += VagrantShellTrailer
			result.append(formatted)

		return result

	def buildMachineConfig(self, instanceConfig):
		def format_string_attr(ruby_var_name, attr_name):
			object = instanceConfig
			for n in attr_name.split('.'):
				object = getattr(object, n, None)
				if object is None:
					return

			assert(type(object) == str)
			result.append("%s = '%s'" % (ruby_var_name, object))

		result = []
		format_string_attr("config.vm.box", "vagrant.image")
		format_string_attr("config.vm.hostname", "name")
		format_string_attr("config.ssh.private_key_path", "keyfile")
		format_string_attr("twopence_platform", "platform.name")
		format_string_attr("twopence_vendor", "platform.vendor")
		format_string_attr("twopence_os", "platform.os")
		format_string_attr("twopence_arch", "platform.arch")

		return result

	def startInstance(self, instance):
		if instance.running:
			print("Cannot start instance %s - already running" % instance.name)
			return False

		when = time.ctime()
		timeout = instance.config.vagrant.timeout or 120

		print("Starting %s instance (timeout = %d)" % (instance.name, timeout))
		status = self.runShellCmd("vagrant --no-tty up", cwd = instance.workspace, timeout = timeout)

		verbose("Saving output to vagrant_up.log")
		instance.saveExecStatus("vagrant_up.log", status)

		if status.exit_code != 0:
			print("Cannot start instance %s - vagrant up failed (%s)" % (instance.name, status))
			return False

		import re

		for line in status.output:
			if "generated public key" in line:
				verbose("Vagrant created a new key for this instance - capturing it")
				path = os.path.join(instance.workspace, ".vagrant/machines/default/libvirt/private_key")
				if os.path.exists(path):
					instance.config.captureKey(path)
				else:
					debug("%s does not exist" % path)
				continue

			if "SSH address" not in line:
				continue

			m = re.match(".*SSH address[: ]*(\d+\.\d+\.\d+\.\d+):(\d+).*", line)
			if m:
				address = m.group(1)
				verbose("Detected SSH address %s" % address)
				instance.addNetworkInterface(Network.AF_IPv4, address)
			else:
				print("Bad: unable to parse address in output of \"vagrant up\"")
				print("  ->> %s" % line.strip())

		instance.recordStartTime(when)
		instance.recordKeyfile(instance.config.keyfile)

		return True

	def updateInstanceTarget(self, instance):
		target = None

		if instance.exists:
			addr = instance.getFirstNetworkAddress(Network.AF_IPv4)
			if addr:
				# hard-coded for now
				target = "tcp:%s:4000" % addr

		instance.recordTarget(target)

	def detectInstanceState(self, instance):
		status = self.runVagrant("status --machine-readable", instance, quiet = True)
		if not status:
			# We could fall back to using virsh directly...
			raise ValueError("%s: vagrant status failed: %s" % (instance.name, status))

		for line in status.output:
			if not line:
				continue

			(ts, name, what, rest) = line.split(',', maxsplit = 3)
			if what == 'state' and name == 'default':
				instance.setStateFromVagrantStatus(rest)

		return True

	def detectInstanceNetwork(self, instance):
		# unfortunately, "vagrant ssh-config" dies on me with a timeout...
		return False

	def stopInstance(self, instance, force = False, destroy = False):
		if destroy:
			return self.destroyInstance(instance)

		if not instance.running and not force:
			debug("not stopping instance %s - already stopped" % instance.name)
			return

		verbose("Stopping %s instance" % instance.name)
		status = self.runVagrant("halt", instance, timeout = 30)
		if not status:
			raise ValueError("%s: vagrant halt failed: %s" % (instance.name, status))

		self.detectInstanceState(instance)
		if instance.running:
			print("%s: vagrant halt failed to stop VM" % instance.name)
			return False

		instance.recordStartTime(None)
		return True

	def destroyInstance(self, instance):
		verbose("Destroying %s instance" % instance.name)
		status = self.runVagrant("destroy -f", instance, timeout = 30)
		if not status:
			raise ValueError("%s: vagrant destroy failed: %s" % (instance.name, status))

		shutil.rmtree(instance.workspace)
		instance.dead = True
		instance.exists = False

		return True

	def saveInstanceImage(self, instance, platform):
		# It seems vagrant package --output does not like absolute path names...
		imageFile = "%s.box" % platform.name
		imagePath = os.path.join(instance.workspace, imageFile)

		verbose("Writing image as %s" % imageFile)
		cmd = "vagrant --machine-readable package --output %s" % imageFile
		status = self.runShellCmd(cmd, cwd = instance.workspace, timeout = 120)
		if not status:
			raise ValueError("%s: vagrant package failed: %s" % (instance.name, status))

		# Copy the box file from workspace to ~/.twopence/data/vagrant/
		return platform.saveImage("vagrant", imagePath)

	def saveInstanceMeta(self, instance, platform, imagePath):
		meta = {
			'name': platform.name,
			'description': 'Irrelevant Description',
			'versions': [
				{
					"version": platform.makeImageVersion(),
					"providers": [
						{
							"name": "libvirt",
							"url": imagePath,
						}
					]
				}
			]
		}

		metaPath = os.path.join(instance.workspace, "%s.json" % platform.name)
		verbose("Writing image metadata as %s" % metaPath)
		with open(metaPath, "w") as f:
			json.dump(meta, f, indent = 4)

		# Copy the json file from workspace to ~/.twopence/data/vagrant/
		return platform.saveImage("vagrant", metaPath)

	def packageInstance(self, instance, packageName):
		assert(instance.config.buildResult)
		platform = instance.config.buildResult
		platform.name = packageName
		platform.build_time = time.strftime("%Y-%m-%d %H:%M:%S GMT", time.gmtime())

		imagePath = self.saveInstanceImage(instance, platform)
		metaPath = self.saveInstanceMeta(instance, platform, imagePath)

		# Inside the platform {} decl, create backend specific info:
		#	backend vagrant {
		#		image "blah";
		#		url "~/.twopence/data/blah.box";
		#	}
		platform.addBackend(self.name, image = platform.name, url = metaPath)

		platform.finalize()

		platform.save()
		return True

	##################################################################
	# List the available boxes. Output looks like this:
	# 1638868423,,ui,info,SLES15-SP3 (libvirt%!(VAGRANT_COMMA) 0)
	# 1638868423,,box-name,SLES15-SP3
	# 1638868423,,box-provider,libvirt
	# 1638868423,,box-version,0
	# ... repeat
	##################################################################
	def listBoxes(self):
		if self.listing:
			return self.listing

		# vagrant --machine-readable box list
		status = self.runShellCmd("vagrant box --machine-readable list", quiet = True)
		if not status:
			# We could fall back to using virsh directly...
			raise ValueError("vagrant box list failed: %s" % (status))

		self.listing = VagrantBoxListing()
		current = None
		for line in status.output:
			if not line:
				continue

			(ts, name, what, rest) = line.split(',', maxsplit = 3)
			if what == 'ui':
				current = self.listing.create()
			elif what == 'box-name':
				current.name = rest
			elif what == 'box-version':
				current.version = rest
				# 0 means no version provided
				if current.version == "0":
					current.version = None
			elif what == 'box-provider':
				current.provider = rest

		return self.listing

	##################################################################
	# Add a box from the given image
	##################################################################
	def addImage(self, box):
		verbose("Adding vagrant box %s from %s" % (box, box.url))
		cmd = "vagrant --no-tty box add --name \"%s\" --provider %s \"%s\"" % (box.name, box.provider, box.url)
		if not self.runShellCmd(cmd, timeout = 60):
			raise ValueError("Failed to add box %s from %s" % (box.name, box.url))

		# Clear any cached listing
		self.listing = None

	##################################################################
	# Run a vagrant command inside an instance workspace
	##################################################################
	def runVagrant(self, subcommand, instance, retries = 3, **kwargs):
		for i in range(retries):
			command = "vagrant "
			if "--machine-readable" not in subcommand:
				command += "--no-tty "
			command += subcommand

			status = self.runShellCmd(command, cwd = instance.workspace, **kwargs)
			if status:
				break

			verbose("vagrant %s failed, retrying" % subcommand)

		return status

	def runShellCmd(self, *args, **kwargs):
		return self.runner.run(*args, **kwargs)
