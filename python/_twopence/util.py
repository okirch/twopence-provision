##################################################################
#
# Utility classes for twopence provisioner
#
# Copyright (C) 2021 Olaf Kirch <okir@suse.de>
#
##################################################################

import susetest
import curly
import sys
import os
import time

opt_verbose = 1

class ProgressBar:
	def __init__(self, message):
		self.message = message
		self.printed = False
		self.enabled = True

	def __del__(self):
		self.finish()

	def disable(self):
		self.enabled = False

	def tick(self):
		if not self.enabled:
			return

		if not self.printed:
			sys.stdout.write(self.message + " ")
			self.printed = True
		sys.stdout.write(".")
		sys.stdout.flush()

	def finish(self, msg = "done"):
		if self.printed:
			print(" %s" % msg)
			self.printed = False

