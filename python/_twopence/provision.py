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

class Provisioner:
	def processTemplate(self, nodeConfig, templatePath, outputPath, extraCommands = []):
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
		d = {}

		d['NAME'] = nodeConfig.name
		d['HOSTNAME'] = nodeConfig.name
		d['PLATFORM'] = nodeConfig.platform.name
		d['IMAGE'] = nodeConfig.image or ""
		d['KEYFILE'] = nodeConfig.keyfile or ""
		d['REPOSITORIES'] = list_sepa.join(repo.url for repo in nodeConfig.repositories)
		d['INSTALL'] = list_sepa.join(nodeConfig.install)
		d['START'] = list_sepa.join(nodeConfig.start)

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
				cmdlist.append("rpm --import " + keyfile)
			else:
				raise NotImplementedError("Cannot upload keyfile to instance")

			cmdlist.append("zypper ar %s %s" % (repo.url, repo.name))
		d['ADD_REPOSITORIES'] = cmdlist

		if nodeConfig.install:
			d['INSTALL_PACKAGES'] = "zypper in -y " + " ".join(nodeConfig.install)
		else:
			d['INSTALL_PACKAGES'] = ""

		d['COMMANDS'] = []

		return d
