##################################################################
#
# Vagrant backend for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################
import os

from .logging import *
from .backend import Backend
from .runner import Runner
from .instance import *
from .provision import *
from .config import Config

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

class VagrantBackend(Backend):
	name = "vagrant"

	def __init__(self):
		debug("Created vagrant backend")

		self.template = None
		self.runner = Runner()
		self.provisioner = Provisioner()

	def configure(self, config):
		self.update_value(config, 'template')

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

	def prepareInstance(self, workspace, instanceConfig, savedInstanceState):
		assert(workspace)

		if not self.template:
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

		extraCommands = [
			# This tells the twopence server where to listen for incoming
			# connections.
			"echo 'port tcp { port 4000; }' >/etc/twopence/ports.conf",
#			"rm -f /etc/twopence/twopence.conf",
		]

		self.provisioner.processTemplate(instanceConfig, self.template, path, extraCommands)

		return VagrantInstance(instanceConfig, instanceWorkspace, savedInstanceState)

	def startInstance(self, instance):
		if instance.running:
			print("Cannot start instance %s - already running" % instance.name)
			return False

		when = time.ctime()

		print("Starting %s instance" % instance.name)
		status = self.runShellCmd("vagrant up", cwd = instance.workspace, timeout = 120)

		verbose("Saving output to vagrant_up.log")
		instance.saveExecStatus("vagrant_up.log", status)

		if status.exit_code != 0:
			print("Cannot start instance %s - vagrant up failed" % instance.name)
			if status.output and verbose_enabled():
				verbose("-- COMMAND OUTPUT --")
				verbose(status.output)
				if not status.output.endswith('\n'):
					verbose()
				print("-- END COMMAND OUTPUT --")

			return False

		import re

		for line in status.output_lines:
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

		for line in status.output_lines:
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

		import shutil

		shutil.rmtree(instance.workspace)
		instance.dead = True
		instance.exists = False

		return True

	def runVagrant(self, subcommand, instance, retries = 3, **kwargs):
		for i in range(retries):
			status = self.runShellCmd("vagrant " + subcommand, cwd = instance.workspace, **kwargs)
			if status:
				break

			verbose("vagrant %s failed, retrying" % subcommand)

		return status

	def runShellCmd(self, *args, **kwargs):
		return self.runner.run(*args, **kwargs)
