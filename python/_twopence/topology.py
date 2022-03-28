##################################################################
#
# Main class for the twopence provisioner: the topology class
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import susetest
import curly
import os
import time

from twopence import ConfigError
from .config import Config
from .instance import *
from .logging import *
from .persist import PersistentTestTopology

class TestTopology(PersistentTestTopology):
	def __init__(self, backend, config = None, workspace = None):
		path = self.makePersistentStatePath(config)
		super().__init__(path)

		# self.configure(config)

		self.backend = backend
		self.backendName = backend.name
		self.workspace = workspace

		self.platform = None
		self._parameters = {}

		self.instanceConfigs = []
		self.instances = []

		if config:
			self.configure(config)

		if not os.path.isdir(self.workspace):
			os.makedirs(self.workspace)

		assert(self.persistentState)

		# Write back persistent state if it does not exist.
		if not os.path.isfile(path):
			self.saveStatus()

		# crap?
		backend.testcase = self.testcase
		# assert(self.testcase)

	def makePersistentStatePath(self, config):
		path = config.status
		if path is None:
			path = os.path.join(self.workspace, "status.conf")
		return path

	def configure(self, config):
		config.validate()

		self.testcase = config.testcase
		self.workspace = config.workspace
		self.logspace = config.logspace

		for node in config.nodes:
			self.createInstanceConfig(node, config)

		if config.parameters:
			self.parameters.update(config.parameters)

		config.configureBackend(self.backend)

	def saveStatus(self):
		self.persistentState.save()

	@property
	def persistentState(self):
		return self._backingObject

	def cleanupStatus(self):
		if self.persistentState:
			self.persistentState.remove()

	def hasRunningInstances(self):
		return any(i.running for i in self.instances)

	def createInstance(self, instanceConfig):
		instanceWorkspace = os.path.join(self.workspace, instanceConfig.name)
		instanceState = self.persistentState.createNodeState(instanceConfig.name)
		return self.backend.createInstance(instanceConfig, instanceWorkspace, instanceState)

	def createAllInstances(self, includeStaleInstances = False):
		found = []

		for instanceConfig in self.instanceConfigs:
			instance = self.createInstance(instanceConfig)
			found.append(instance)

		if includeStaleInstances:
			# Loop over all nodes defined in status.conf - the user may have messed with the test config
			# file and added/removed nodes.
			for savedInstanceState in self.persistentState.nodes:
				if any(instance.name == savedInstanceState.name for instance in found):
					continue

				dummyConfig = Config.createEmptyNode(savedInstanceState.name)
				instance = self.createInstance(dummyConfig)
				found.append(instance)

		return found

	def detect(self, detectNetwork = False):
		found = self.createAllInstances(includeStaleInstances = True)
		self.instances = self.backend.detect(self, found)
		return self.instances

	def requires(self):
		for instanceConfig in self.instanceConfigs:
			for name in instanceConfig.requires:
				yield name, instanceConfig.name

	def prepare(self):
		assert(not self.instances)

		self.saveStatus()

		success = True
		instances = self.createAllInstances()

		for instance in instances:
			if not self.backend.downloadImage(instance):
				raise ValueError(f"Failed to download image for instance {instance.name}")

		for instance in instances:
			# Create the workspace directory of this instance.
			# This throws an exception in case of errors
			instance.createWorkspace()

			if instance.config.platform.isApplication:
				self.backend.prepareApplication(instance)
			else:
				self.backend.prepareInstance(instance)

			if instance.exists:
				error("Ouch, instance %s seems to exist" % instance.name)
				success = False

			self.instances.append(instance)

		self.saveStatus()
		return success

	def start(self, okayIfRunning = False):
		if any(i.exists for i in self.instances):
			print("Refusing to start; please clean up any existing instances first");
			return False

		success = True
		for instance in self.instances:
			if instance.running:
				if not okayIfRunning:
					raise ValueRrror("Instance %s already running" % instance.name)
				continue

			if verbose_enabled():
				verbose("  Image %s, SSH keyfile %s" % (instance.config.image, instance.config.keyfile))
				if instance.config.install:
					verbose("  Installing package(s):")
					for name in instance.config.install:
						verbose("        %s" % name)
				if instance.config.start:
					verbose("  Starting service(s):")
					for name in instance.config.start:
						verbose("        %s" % name)

			if not instance.persistent:
				print("Oops, no persistent state for %s?!" % instance.name)
				fail

			try:
				success = self.backend.startInstance(instance)
			except Exception as e:
				import traceback

				print("Caught exception while trying to start instance: %s" % e)
				traceback.print_exc()
				success = False

			if not success:
				print("Failed to start instance %s" % instance.name)
				break

			instance.exists = True
			instance.running = True

			instance.propagateRuntimeResources()

			self.backend.updateInstanceTarget(instance)

			self.saveStatus()

		return success

	def stop(self, **kwargs):
		for instance in self.instances:
			instance.stopTwopence()

			self.backend.stopInstance(instance, **kwargs)
			self.backend.updateInstanceTarget(instance)

			self.saveStatus()

	def package(self, nodeName, packageName):
		instance = self.getInstance(nodeName)
		if instance is None:
			raise ValueError(f"Cannot package {nodeName}: instance not found")

		platform = instance.createBuildResult(packageName)
		if platform is None:
			raise ValueError(f"Cannot package {nodeName}: couldn't create build-result platform")

		self.backend.packageInstance(instance, platform)

		# FIXME: finalize is currently a no-op
		platform.finalize()
		platform.save()

		return platform

	def destroy(self):
		for instance in self.instances:
			self.backend.destroyInstance(instance)

			instance.destroyRuntime()

			if instance.persistent:
				self.persistentState.dropNode(instance.persistent)

			self.saveStatus()
		self.instances = []

	def cleanup(self):
		self.cleanupStatus()

		# Do not try to remove the workspace; it contains the BOM file
		# and possibly copies of some config files

	def createInstanceConfig(self, node, config):
		nodeConfig = config.finalizeNode(node, self.backend)
		self.instanceConfigs.append(nodeConfig)
		return nodeConfig

	def getInstance(self, name):
		for instance in self.instances:
			if instance.name == name:
				return instance
		return None
