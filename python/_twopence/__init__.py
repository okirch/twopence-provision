##################################################################
#
# module __init__ file for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

from .runner import Runner
from .util import Configurable
from .logging import *

class ConfigError(Exception):
	pass

class Backend(Configurable):
	@staticmethod
	def create(family):
		if family == 'vagrant':
			from .vagrant import VagrantBackend

			return VagrantBackend()

		raise ConfigError("Cannot create backend \"%s\" - unknown backend family" % family)

from .topology import TestTopology
from .manifest import BOM
