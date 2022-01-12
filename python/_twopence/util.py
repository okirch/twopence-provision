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

class DottedNumericVersion:
	def __init__(self, version_string):
		self._value = version_string
		if version_string is None:
			self._parsed = []
		else:
			self._parsed = [int(_) for _ in version_string.split('.')]

	def __str__(self):
		return self._value

	def __eq__(self, other):
		return self._value == other._value
	def __ne__(self, other):
		return self._value != other._value

	def __lt__(self, other):
		return self.compare(other) < 0
	def __le__(self, other):
		return self.compare(other) <= 0
	def __gt__(self, other):
		return self.compare(other) > 0
	def __ge__(self, other):
		return self.compare(other) >= 0

	# return
	#  <0 iff a < b
	#   0 iff a == b
	#  >0 iff a > b
	def compare(self, other):
		d = self._compare(other)
		if d < 0:
			return -1
		if d > 0:
			return 1
		return d

	def _compare(self, other):
		if self._value == other._value:
			return 0

		for a, b in zip(self._parsed, other._parsed):
			if a == b:
				continue

			return a - b

		return len(A) - len(B)
