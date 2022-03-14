#
# Copyright (C) 2021 Olaf Kirch <okir@suse.com>
#
# Quick and dirty - this is some old code of mine which I just need
# for checking a registry for available image versions. Need to clean
# this up.
#

import sys
import json
import os.path
import copy
import io
import re
import tempfile
import base64
import urllib.parse
import urllib.request
from urllib.error import HTTPError

from twopence import logger, info, debug, debug_extra, error

class ImageLoadError(Exception):
	def __init__(self, image, reason):
		self.image = image
		self.reason = reason

class ImageSaveError(Exception):
	def __init__(self, image, reason):
		self.image = image
		self.reason = reason

class LoginError(Exception):
	def __init__(self, image, reason):
		self.image = image
		self.reason = reason

# This is used to query a registry for an image
class ImageReference:
	def __init__(self, registry, name, architecture = 'amd64', url = None):
		self.registry = registry or "localhost"
		if ':' in name:
			name, tag = name.rsplit(':', maxsplit = 1)
		else:
			tag = "latest"

		self.name = name.strip("/")
		self.tag = tag
		self.architecture = architecture

		self._url = url

	def __str__(self):
		return f"{self.registry}/{self.name}:{self.tag}"

	def __eq__(self, other):
		return str(self) == str(other)

	def matchImage(self, image):
		if self.tag and image.tag != self.tag:
			return False
		return self.name in image.names

	@property
	def url(self):
		if self._url is None and self.registry:
			self._url = self.guessURL(self.registry)
		return self._url

	@staticmethod
	def parse(path):
		url = ImageReference.guessURL(path)
		# print(f"{path} -> {url}")
		return ImageReference(url.netloc, url.path, url = url)

	@staticmethod
	def guessURL(path):
		url = ImageReference.tryThisURL(path)
		if url is None:
			url = ImageReference.tryThisURL("//" + path)
		if url is None:
			url = ImageReference.tryThisURL(ImageFormatFactory.defaultRegistryURL + "/" + path)
		if url is None:
			raise ValueError(f"Cannot parse registry image name \"{path}\"")

		# debug(f"url = {url}")
		return url

	@staticmethod
	def tryThisURL(path):
		from urllib.parse import urlparse
		import socket

		# debug("tryThisURL(%s)" % path)

		url = urlparse(path, scheme = 'https')
		if not url.netloc:
			return None

		try:
			socket.gethostbyname(url.netloc)
		except:
			# Not a valid hostname
			return None

		return url


class ImageManifest(object):
	def __str__(self):
		version = self.imageVersion
		if version is None:
			version = self.tag

		return f"{self.__class__.__name__}({self.name}, version={version}, id={self.imageID})"

	def getConfig(self):
		return None

	@property
	def configDescriptor(self):
		return None

class ImageManifestV1(ImageManifest):
	def __init__(self, schemaVersion = 1, name = None, tag = None, architecture = None, history = None, signatures = None, fsLayers = []):
		self.schemaVersion = schemaVersion
		self.name = name
		self.tag = tag
		self.architecture = architecture
		self.history = history

		# TBD: process fsLayers
		self._layers = []

		self.imageVersion = None
		self.imageID = None

		for entry in history or []:
			v1 = entry.get('v1Compatibility')
			v1 = json.loads(v1)

			version = None

			chase = v1
			for name in ("config", "Labels", "org.opencontainers.image.version"):
				if chase:
					chase = chase.get(name)
			self.imageVersion = chase
			self.imageID = v1.get('id')

	def getConfig(self):
		# parse the config we were given
		pass

class ImageManifestV2(ImageManifestV1):
	def __init__(self, schemaVersion = 1, name = None, tag = None, architecture = None, mediaType = None, config = None, layers = []):
		self.name = name
		self.tag = tag
		self.architecture = architecture
		self.schemaVersion = schemaVersion
		self.mediaType = mediaType
		if config is None:
			self._config = None
		else:
			self._config = OCIDescriptor(**config)
		self._layers = [OCIDescriptor(**l) for l in layers]

		self.imageVersion = None
		self.imageID = None

	def getConfig(self):
		return None

	@property
	def configDescriptor(self):
		return self._config

class ImageConfig(object):
	def __init__(self, config = None, **other):
		self._config = config
		self.other = other

	def _get(self, key, defaultValue = None):
		if not self._config:
			return defaultValue
		return self._config.get(key, defaultValue)

	@property
	def id(self):
		return self._get('Id')

	@property
	def labels(self):
		return self._get('Labels', {})

	@property
	def imageVersion(self):
		return self.labels.get('org.opencontainers.image.version')

	@property
	def imageNames(self):
		return list(map(lambda s: ImageReference.parse(s), self._get('Names', [])))

class ImageIndex(object):
	def __init__(self, schemaVersion = 2, mediaType = None, manifests = []):
		self.schemaVersion = schemaVersion
		self.mediaType = mediaType
		self.manifests = [OCIDescriptor(**mf) for mf in manifests]

	def find(self, architecture = None):
		wildcard = None
		for mf in self.manifests:
			platform = getattr(mf, 'platform', None)
			if platform is None:
				if wildcard is not None:
					raise ValueError("Image index lists more than one descriptor without platform")
				wildcard = mf
				continue

			if platform['architecture'] == architecture:
				return mf

		return wildcard

from types import SimpleNamespace

class OCIDescriptor(SimpleNamespace):
	def __init__(self, **d):
		super(OCIDescriptor, self).__init__(**d)

		mediaType = getattr(self, 'mediaType', None)
		if mediaType is not None:
			self._parsedMediaType = MediaType(mediaType)

	def addURL(self, url):
		urlList = getattr(self, 'urls', [])
		urlList.append(url)
		self.urls = urlList

	def isImageIndex(self):
		return self._parsedMediaType.isImageIndex()

	def isManifest(self):
		return self._parsedMediaType.isManifest()

	def isImageLayer(self):
		return self._parsedMediaType.isImageLayer()

	def isCompressed(self):
		return self._parsedMediaType.compression is not None

	def asUncompressed(self, img):
		import hashlib

		f = img.openLayerBlob(self, uncompress = True)

		hash = hashlib.new(self.digestAlgorithm())
		size = 0
		while True:
			data = f.read(65536)
			if not data:
				break
			hash.update(data)
			size += len(data)

		ret = copy.deepcopy(self)
		ret._parsedMediaType.compression = None
		ret.mediaType = repr(ret._parsedMediaType)
		ret.size = size

		ret.digest = "%s:%s" % (hash.name, hash.hexdigest())

		return ret

	def isExternalReference(self):
		return self._parsedMediaType.isExternalReference()

	def asExternalReference(self, img):
		ret = copy.deepcopy(self)

		# Change the mediaType to an external reference and set the URLs
		ret.makeExternalReference()
		ret.addURL(img.loader.blobURL(img, self).geturl())

		return ret

	def makeExternalReference(self):
		self._parsedMediaType.makeExternalReference()
		self.mediaType = repr(self._parsedMediaType)

	def digestAlgorithm(self):
		assert(':' in self.digest)
		return self.digest.split(':')[0]

	def vendor(self):
		return self._parsedMediaType.vendor

	def sameVendor(self, vendor):
		return self._parsedMediaType.vendor == vendor

	def asVendor(self, vendor):
		ret = copy.deepcopy(self)
		ret._parsedMediaType.changeVendor(vendor)
		ret.mediaType = repr(ret._parsedMediaType)

		print("  Converted %s -> %s for vendor compatibility" % (self.mediaType, ret.mediaType))
		return ret

	def platformString(self):
		try:
			platform = self.platform
		except:
			return "any platform"

		arch = platform['architecture']
		os = platform.get('os')
		if os is None:
			return arch

		return os + "/" + arch

class JSONObjectEncoder(json.JSONEncoder):
	def default(self, o):
		assert(isinstance(o, object))

		d = dict()
		for key in dir(o):
			if key.startswith('_'):
				continue

			value = getattr(o, key)
			if callable(value) or value is None:
				continue

			d[key] = value

		return d

class KeystoreBase(object):
	class Credentials:
		def __init__(self, user, password, url = None):
			self.user = user
			self.password = password
			self.url = url

	def get(self, host):
		return None

class Keystore(urllib.request.HTTPPasswordMgr):
	def __init__(self):
		self._stores = []
		self._stores.append(DockerPlaintextKeystore())

	def get(self, host):
		for s in self._stores:
			creds = s.get(host)
			if creds:
				return creds
		return None

class AuthenticationRequired(Exception):
	def __init__(self, auth_req):
		self.auth_req = auth_req

class HTTP:
	@staticmethod
	def logResponse(req, fp, code, msg, headers, logfn):
		if not logfn.enabled:
			return

		ti = logger.incrementIndent()
		logfn("HTTP response %d (%s)" % (code, msg))
		for k, v in headers.items():
			logfn(f"  {k}: {v}")

		HTTP.logBody(fp, headers, logfn)

	@staticmethod
	def logError(req, fp, code, msg, headers, logfn):
		if not logfn.enabled:
			return

		logfn("Received HTTP error %d (%s)" % (code, msg))
		for k, v in headers.items():
			logfn(f"  {k}: {v}")

		if headers.get('content-type') == 'application/json':
			try:
				data = json.load(fp)
			except:
				logfn("Unable to parse JSON body")
				return

			logfn("JSON body")
			for k, v in data.items():
				logfn(f"  {k}: {v}")

	def logBody(fp, headers, logfn):
		try:
			pos = fp.tell()
		except:
			logfn("  Not trying to parse body (stream is not seekable)")
			return

		contentType = headers.get('content-type')
		if contentType == 'application/json' or \
		   contentType.endswith('+json'):
			try:
				data = json.load(fp)
				fp.seek(pos)
			except:
				logfn("  Unable to parse JSON body")
				return

			logfn("  JSON body")
			for k, v in data.items():
				logfn(f"    {k}: {v}")

# When trying to push to docker registry w/o authentication, it will return a 401 error
# with the following Www-Authenticate header:
# Www-Authenticate: Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:okir/test:pull"
#
class BearerAuthHandler(urllib.request.BaseHandler):
	class AuthRequest:
		def __init__(self, type):
			self.type = type
			self.params = dict()

		@property
		def realm(self):
			return self.params.get('realm')

		@property
		def scope(self):
			return self.params.get('scope')

		@scope.setter
		def scope(self, value):
			self.params['scope'] = value

		@property
		def service(self):
			return self.params.get('service')

		@property
		def queryString(self):
			def encode(v):
				v = v.replace(' ', '%20')
				return v

			return "&".join(['%s=%s' % (k, encode(v)) for k, v in self.params.items()])

		def __str__(self):
			return "%s %s" % (self.type,
				",".join(['%s=%s' % (k, v) for k, v in self.params.items()]))

	installed = False

	@staticmethod
	def install():
		if not BearerAuthHandler.installed or True:
			debug_extra("    Installing urllib handler for Bearer authentication")
			authhandler = BearerAuthHandler()
			opener = urllib.request.build_opener(authhandler)
			urllib.request.install_opener(opener)
			BearerAuthHandler.installed = True

	def http_error_401(self, req, fp, code, msg, headers):
		HTTP.logError(req, fp, code, msg, headers, logfn = debug_extra)

		auth_header = headers.get('www-authenticate')
		if not auth_header:
			raise ValueError("%s: missing Www-Authenticate in 401 error" % self.__class__.__name__)

		auth_req = self.parse_auth_header(auth_header)
		if not auth_req:
			raise ValueError("%s: cannot parse Www-Authenticate header: %s" % (self.__class__.__name__, auth_header))

		# print("Parsed auth req %s" % auth_req)

		if auth_req.type != 'Bearer':
			debug("  Ignoring auth header with type \"%s\"" % auth_req.type)
			return

		# An error indicates we've been presenting a Bearer token, but it was
		# not sufficient or wrong.
		# If it was not sufficient, we should at least retry...
		error = auth_req.params.get('error')
		if error and error != 'insufficient_scope':
			raise HTTPError(req.full_url, code, msg, headers, None)

		raise AuthenticationRequired(auth_req)

	def parse_auth_header(self, auth_header):
		# print("Parsing auth header \"%s\"" % auth_header)
		m = re.match("\s*(\S+)\s+(.*)", auth_header)
		if not m:
			return None

		auth_req = self.AuthRequest(m.group(1))
		param_string = m.group(2)

		while param_string:
			# print("param_string=\"%s\"" % param_string)
			m = re.match("([^=]+)=\"([^\"]*)\"(.*)", param_string)
			if not m:
				m = re.match("([^=]+)=([^,]*)(.*)", param_string)

			if not m:
				return None

			(key, value, rest) = m.groups()

			auth_req.params[key] = value
			# print("      %s=%s" % (key, value))

			param_string = rest.lstrip(",")

		return auth_req

	# def http_error_auth_reqed(authreq, host, req, headers):
	def http_error_auth_reqed(**args):
		print("http_error_auth_reqed(%s)" % args)
		foop

class DockerPlaintextKeystore(KeystoreBase):
	def __init__(self):
		import os

		path = "%s/.docker/config.json" % os.getenv("HOME")
		if not os.path.isfile(path):
			self.data = None

		with open(path) as f:
			self.data = json.load(f)

	def get(self, host):
		d = self.data
		for key in ('auths', host, 'auth'):
			d = d.get(key)
			if d is None:
				return None

		auth = base64.b64decode(d).decode()

		if ':' not in auth:
			return None

		(user, password) = auth.split(':', 1)
		return self.Credentials(user, password)

class ImageFormat(object):
	def __init__(self):
		self._cache = None
		self.architecture = None
		self._keystore = None

	def setArchitecture(self, arch):
		self.architecture = arch

	def setCacheDir(self, path):
		self._cache = ImageBlobCache(path)

	def setKeystore(self, keystore):
		self._keystore = keystore

	def spec(self):
		raise NotImplementedError("%s: method spec() not implemented" % self.__class__.__name__)

	@staticmethod
	def splitImageSpec(spec):
		assert(':' in spec)
		return spec.split(':', 1)

	def writeString(self, f, versionString):
		print(versionString, file = f)

	def parseImageIndex(self, f, missingMediaTypeOK = False):
		return self.loadJSON(f, ImageIndex, MediaType.indexValidator(missingMediaTypeOK))

	def writeImageIndex(self, f, mfl):
		self.storeJSON(f, mfl)

	def pickManifestFromIndex(self, mfList, architecture = "amd64"):
		# Find the manifest matching the desired architecture
		desc = mfList.find(architecture = architecture)
		if not desc or not desc.isManifest:
			info("Index does not contain a compatible manifest")
			return None

		# info("Using manifest for %s: %s" % (desc.platformString(), desc.digest))
		return desc

	def parseManifest(self, f):
		validator = MediaType.Validator(defaultHandler = ImageManifestV1)
		validator.acceptMediaTypes('Manifest', ImageManifestV2)

		mf = self.loadJSON(f, validator)
		if mf.schemaVersion != 2:
			raise ValueError(f"Unexpected manifest schemaVersion {mf.schemaVersion}")
		return mf

	def parseManifestOrIndex(self, f):
		# Possible server responses:
		# - no media type: this is a manifest
		# - single manifest
		# - image index
		validator = MediaType.Validator(defaultHandler = ImageManifest)
		validator.acceptMediaTypes('Manifest', ImageManifest)
		validator.acceptMediaTypes('ImageIndex', ImageIndex)

		result = self.loadJSON(f, validator)
		if isinstance(result, ImageManifest):
			if result.schemaVersion != 1:
				raise ValueError(f"Unexpected manifest schemaVersion {result.schemaVersion}")
		else:
			pass

		return result

	def writeManifest(self, f, mf):
		self.storeJSON(f, mf)

	def parseConfig(self, f):
		validator = MediaType.Validator(defaultHandler = ImageConfig)
		validator.acceptMediaTypes('Config', ImageConfig)
		return self.loadJSON(f, validator)

	def loadConfig(self, img, d):
		with self.openBlob(img, d, mode = "r") as f:
			return self.parseConfig(f)

	def loadJSON(self, f, validator = None):
		# Parse JSON into an object with attributes corresponding to dict keys.
		data = json.load(f)

		if not validator:
			return data

		# print(data)
		handler = validator.validateJSON(data)
		return handler(**data)

	# Default save() operation
	# We save all blobs first, and then the manifest, because that's
	# compatible with the sequence of uploads to a docker registry.
	def save(self, img):
		self.savePreamble(img)

		self.saveConfig(img, img.manifest.config)
		self.saveImageLayers(img)

		self.saveManifest(img, img.manifest)

	def savePreamble(self, img):
		pass

	def saveConfig(self, img, cfg):
		info("Writing config")
		self.copyBlob(img, cfg)

		if not self.blobExists(img, cfg):
			raise ImageSaveError(self.spec(), "Uploaded config, but it's not available?!")

	def saveImageLayers(self, img):
		info("Writing image layers")
		for d in img.manifest.layers:
			self.saveLayer(img, d)

	def saveLayer(self, img, d):
		s = logger.incrementIndent()
		info("Saving layer %s (%s)" % (d.digest, d.mediaType))
		if d.isExternalReference():
			info("  Skipping external reference; urls=%s" % (d.urls))
		else:
			self.copyBlob(img, d)


	def storeJSON(self, f, obj):
		json.dump(obj, cls = JSONObjectEncoder, fp = f, indent = "\t")

	def blobURL(self, img, d):
		raise NotImplementedError("%s: method blobURL() not implemented" % self.__class__.__name__)

	def blobFilesystemPath(self, d):
		return None

	def tryToHardlinkBlob(self, loader, d):
		return False

	def blobExists(self, img, d):
		return False

	def copyBlob(self, img, d):
		if self.blobExists(img, d):
			verbose("  Blob %s already exists, not copying" % d.digest)
			return

		info("Copying blob %s (size %s)" % (d.digest, d.size))

		assert(img.loader)
		loader = img.loader

		if self.tryToHardlinkBlob(loader, d):
			return

		with loader.openBlob(img, d) as f:
			self.saveBlob(img, d, f)

	# Authentication for registries
	# The default is not to log in
	def login(self):
		pass

	def getCredentials(self, realm):
		if not realm:
			return

		registry = RegistryInfo.forHost(realm)
		if not registry:
			print("No registry info for realm %s, not logging in" % realm)
			return

		if not self._keystore:
			print("No keystore, not logging in")
			return

		creds = None
		if registry.cred_url:
			creds = self._keystore.get(registry.cred_url)
		if creds is None:
			creds = self._keystore.get(registry.auth_url)

		if creds is None:
			print("No credentials for %s, not logging in")
			return

		creds.url = registry.auth_url
		return creds

class RegistryInfo(object):
	def __init__(self, realm, auth_url = None, cred_url = None):
		self.realm = realm
		self.auth_url = auth_url
		self.cred_url = cred_url

	@staticmethod
	def forHost(realm):
		if realm == 'docker.io':
			return RegistryInfo(realm,
				auth_url = "https://hub.docker.com/v2/users/login",
				cred_url = "https://index.docker.io/v1/")

		if realm == 'https://auth.docker.io/token':
			return RegistryInfo(realm,
				auth_url = "https://auth.docker.io/token",
				cred_url = "https://index.docker.io/v1/")

		return None

class HTTPContent(object):
	def __init__(self, contentType, content, contentLength = -1):
		self.contentType = contentType

		if contentType == "application/json":
			debug_extra("  Sending JSON data:")
			debug_extra(json.dumps(content, cls = JSONObjectEncoder, indent = "\t"))
			data = json.dumps(content, cls = JSONObjectEncoder).encode('utf-8')
			# print("Content %s" % data)
		elif contentType == "application/octet-stream":
			data = content.read()
		elif contentType.startswith('application/vnd.'):
			data = content
			# print("  content=%s" % data)
		else:
			raise ValueError("Cannot handle HTTP data with content type %s" % contentType)

		if contentLength < 0:
			contentLength = len(data)

		assert(contentLength >= 0)

		self.contentLength = contentLength
		self.content = data

class ImageFormatDockerRegistry(ImageFormat):
	# Expect an image spec including the registry name
	def __init__(self, searchKey):
		super().__init__()

		self.key = searchKey
		self.creds = None

		self.url = searchKey.url

	@property
	def name(self):
		return self.key.name

	@property
	def tag(self):
		return self.key.tag

	def __str__(self):
		desc = self.path
		if self.version:
			desc += ":" + self.version
		return f"ImageFormatDockerRegistry({desc})"

	def spec(self):
		return self.url._replace(scheme = "docker").geturl()

	def login(self):
		creds = self.getCredentials(self.url.netloc)
		if not creds:
			return

		data = {
			'username' : creds.user,
			'password' : creds.password
		}

		url = self.parseValidateURL(creds.url)
		if not url:
			raise LoginError(self.spec(), "Invalid login url \"%s\"" % creds.url)

		debug("Logging into %s" % creds.url)

		resp = self.httpPost(url, "application/json", data, mode = "r")
		with resp as f:
			auth_reply = self.loadJSON(f)
			if not auth_reply.get('token'):
				raise LoginError(self.spec(), "Unexpected response from %s" % creds.url)

			creds.auth_token = auth_reply['token']

		debug("  Successfully authenticated")
		self.creds = creds

	def query(self):
		name = self.name.strip('/')
		tag = self.tag

		url = f"v2/{name}/manifests/{tag}"
		with self.openURL(url) as f:
			found = self.parseManifestOrIndex(f)

			if isinstance(found, ImageManifest):
				return found

			desc = self.pickManifestFromIndex(found)
			if not desc:
				raise FileNotFoundError(f"Cannot find image for {name}:{tag}")

			img = Image(name, tag)

			# Now load the actual manifest
			with self.openManifest(img, desc) as f:
				mf = self.parseManifest(f)

			img.setManifest(mf, self)
			return img

		return None

	def load(self):
		name = self.name.strip('/')
		version = self.version or "latest"

		with self.openURL("v2/%s/manifests/%s" % (name, version)) as f:
			mfList = self.parseImageIndex(f)

		desc = self.pickManifestFromIndex(mfList)
		if not desc:
			raise FileNotFoundError("Cannot find image for %s:%s" % (name, version))

		img = Image(name, version)

		# Now load the actual manifest
		with self.openManifest(img, desc) as f:
			mf = self.parseManifest(f)

		img.setManifest(mf, self)

		return img

	def openURL(self, partialURL, mode = "r", **kwargs):
		return self.download(partialURL, mode = mode, **kwargs)

	def openManifest(self, img, d):
		return self.openURL("v2/%s/manifests/%s" % (img.name, d.digest))

	def blobExists(self, img, d):
		# HEAD /v2/<name>/blobs/<digest>
		return self.httpHead("/v2/%s/blobs/%s" % (self.name, d.digest))

	def openBlob(self, img, d, mode = "rb"):
		if mode.startswith('w'):
			raise ValueError("%s: image push not yet implemented" % self.__class__.__name__)

		cacheHandle = None
		if self._cache:
			cacheHandle = self._cache.createHandleFor(d.digest)

		return self.download("v2/%s/blobs/%s" % (img.name, d.digest), mode, cache = cacheHandle)

	def saveBlob(self, img, d, src):
		self.upload("v2/%s/blobs/uploads/" % self.name.strip("/"), src, d)
		return

	def saveManifest(self, img, src):
		name = self.name.strip("/")
		reference = self.version or "latest"

		src = copy.copy(src)
		src.name = name
		src.tag = reference
		src.architecture = self.architecture or "amd64"

		info("Writing manifest")
		self.uploadManifest("v2/%s/manifests/%s" % (name, reference), src)

	def uploadManifest(self, partialURL, src):
		url = self.makeURL(partialURL)

		# Content is json, but we change the content-type to the
		# media type of the manifest
		content = HTTPContent('application/json', src)
		content.contentType = src.mediaType

		resp = self.httpPost(url, method = 'PUT', content = content)

		# This returns something like:
		# 201 Created
		# Docker-Content-Digest: sha256:<....>
		# Docker-Distribution-Api-Version: registry/2.0
		# Location: https://registry-1.docker.io/v2/<name>/manifests/<digest>
		# Content-Length: 0

		assert(resp.code == 201)

	def doLoginDocker(self, auth_request, creds = None):
		if creds:
			auth_request.params['account'] = creds.user

		realm = auth_request.params.pop('realm')

		req = urllib.request.Request(realm + "?" + auth_request.queryString, method = "GET")

		debug_extra("  %s %s" % (req.method, req.full_url))

		if creds:
			# Authorization: Bearer foobZEGO==
			upass = "%s:%s" % (creds.user, creds.password)
			upass = base64.b64encode(upass.encode("utf-8"))
			hdr = "Basic %s" % (upass.decode("utf-8"))
			req.add_header("Authorization", hdr)

		resp = urllib.request.urlopen(req)
		with resp as f:
			auth_reply = self.loadJSON(f)
			if not auth_reply.get('token'):
				raise LoginError(self.spec(), "Unexpected response from %s" % creds.url)

			if not creds:
				creds = KeystoreBase.Credentials(None, None)

			creds.authorization_header = "%s %s" % (auth_request.type, auth_reply['token'])
			creds.auth_type = auth_request.type
			creds.auth_token = auth_reply['token']
			creds.scope = auth_request.scope

			creds.expires_in = auth_reply.get('expires_in') or '60'
			creds.issued_at = auth_reply.get('issued_at') or 'just now'

			self.displayJWT(creds.auth_token)

		debug("  Successfully authenticated (issued %s, expires in %s seconds)" % (creds.issued_at, creds.expires_in))
		self.creds = creds

	def displayJWT(self, token):
		try:
			import jwt

			payload = jwt.decode(token, verify = False)
		except:
			return True

		access = payload.get('access')
		if not access:
			print("## WARNING: The token returned by the server does not grant any access ##")
			print("  Token payload:")
			for k, v in payload.items():
				print("    %-8s %s" % (k, v))

			return False

		return True

	def blobURL(self, img, d):
		return self.makeURL("v2/%s/blobs/%s" % (img.name, d.digest))

	def makeURL(self, partialURL):
		url = self.url._replace(path = partialURL)

		if url.netloc == 'docker.io':
			url = url._replace(netloc = 'registry-1.docker.io')

		return url

	def download(self, partialURL, mode = "rb", cache = None, accept = []):
		import urllib.request
		from urllib.error import HTTPError
		import io

		url = self.makeURL(partialURL).geturl()

		if cache:
			f = cache.open(mode)
			if f:
				debug("  Using cached data for %s" % url)
				return f

		info("Downloading %s" % url)

		if self.url.netloc == 'registry.suse.com':
			info("  %s requires authentication; don't be too surprised if this download fails" % self.url.netloc)

		req = urllib.request.Request(url, method = "GET")
		for mediaType in accept:
			req.add_header("Accept", mediaType)

		try:
			resp = self.urlopen(req)
		except HTTPError as e:
			raise ImageLoadError(self.path, str(e))

		if resp.status != 200:
			raise ImageLoadError(self.path, str(e))
			raise ValueError("Unable to retrieve %s: status %s (%s)" % (
				url, resp.status, resp.reason))

		if cache:
			cache.put(resp)
			return cache.open(mode)

		if 'b' not in mode:
			return io.StringIO(resp.read().decode())

		return resp

	def upload(self, partialURL, src, d):
		url = self.makeURL(partialURL)

		# POST /v2/<name>/blobs/uploads/
		debug("Uploading to %s" % url.geturl())
		auth_request = None
		fail_reason = None

		try:
			resp = self.httpPost(url)
		except HTTPError as e:
			raise ImageSaveError(self.path, str(e))

		# The server should give us a 202 response with these headers:
		#   Location: /v2/<name>/blobs/uploads/<uuid>
		#   Range: bytes=0-<offset>
		#   Content-Length: 0
		#   Docker-Upload-UUID: <uuid>

		location = resp.headers.get('location')
		if not location:
			raise ImageSaveError(self.path, "Missing Location header in server response")

		url = urllib.parse.urlparse(location)

		digestQ = "digest=%s" % d.digest
		if url.query:
			url = url._replace(query = url.query + "&" + digestQ)
		else:
			url = url._replace(query = digestQ)

		resp = self.httpPost(url, method = "PUT",
				content = HTTPContent('application/octet-stream', src, contentLength = d.size))

		if resp.code != 201:
			raise ImageSaveError(self.path, "Unexpected HTTP response %s to blob upload" % resp.code)

		digest = resp.headers.get('Docker-Content-Digest')
		if digest != d.digest:
			raise ImageSaveError(self.path, "Registry returns a different digest after upload (%s -> %s)" % (
				d.digest, digest))

		return

	def httpPost(self, url, method = "POST", content = None, mode = "rb"):
		import urllib.request
		from urllib.error import HTTPError
		import io

		if content:
			req = urllib.request.Request(url.geturl(), method = method, data = content.content)
		else:
			req = urllib.request.Request(url.geturl(), method = method)

		resp = self.urlopen(req, content)

		# Do not catch HTTP Error - let the caller catch that

		if int(resp.status / 100) != 2:
			raise ImageSaveError(self.path, str(e))

		if 'b' not in mode:
			return io.StringIO(resp.read().decode())

		return resp

	def httpHead(self, partialURL):
		import urllib.request
		from urllib.error import HTTPError

		url = self.makeURL(partialURL).geturl()

		debug("Checking %s" % url)

		req = urllib.request.Request(url, method = "HEAD")

		try:
			resp = self.urlopen(req)
		except HTTPError as e:
			print("HEAD returned failure: %s" % str(e))
			return False

		if resp.status != 200:
			return False

		return True

	def urlopen(self, req, content = None):
		# s = logger.incrementIndent()

		debug(f"{req.method} {req.full_url}")

		try:
			resp = self._urlopen(req, content)
		except HTTPError as e:
			self.showResponse(e, showData = True)
			raise e

		self.showResponse(resp)
		return resp
	
	def showResponse(self, resp, showData = False):
		HTTP.logResponse(None, resp.fp, resp.status, resp.reason, resp.headers, logfn = debug_extra)
		return

	def _urlopen(self, req, content):
		BearerAuthHandler.install()

		if self.creds:
			debug_extra("  Adding header: Authorization: %.80s.." % self.creds.authorization_header)
			req.add_header("Authorization", self.creds.authorization_header)

		if content:
			debug_extra("  Adding header: Content-Type: %s" % content.contentType)
			req.add_header("Content-Type", content.contentType)

			if content.contentLength >= 0:
				debug_extra("  Adding header: Content-Length: %s" % content.contentLength)
				req.add_header("Content-Length", content.contentLength)

		try:
			return urllib.request.urlopen(req)
		except AuthenticationRequired as ar:
			auth_request = ar.auth_req

		creds = self.getCredentials(auth_request.realm)
		if creds is None:
			self.doLoginDocker(auth_request, creds)
		else:
			debug("Authentication requested: %s" % (auth_request))
			if auth_request.realm.startswith("https://auth.docker.io"):
				self.doLoginDocker(auth_request, creds)
			else:
				# FIXME: -> RegistryError
				raise ImageSaveError(self.path, "Don't know how to authenticate (request=%s)" % auth_req)

		debug("Retrying HTTP request")
		debug("  %s %s" % (req.method, req.full_url))

		if self.creds:
			debug_extra("  Adding header: Authorization: %.80s.." % self.creds.authorization_header)
			req.add_header("Authorization", self.creds.authorization_header)

		return urllib.request.urlopen(req)

	def anonSession(self, realm, service, scope):
		url = f"{realm}?scope={scope}&service={service}"
		print(url)

class ImageFormatDir(ImageFormat):
	def __init__(self, path, name = None):
		self.path = path
		self.name = name or os.path.basename(path)
		self.version = None

		self.transportVersion = "1.1"

	def spec(self):
		return "dir:%s" % self.path

	def open(self, relativePath, mode = "r"):
		if mode == "w":
			if not os.path.isdir(self.path):
				os.makedirs(self.path)

		filename = os.path.join(self.path, relativePath)
		return open(filename, mode)

	def load(self):
		with self.open("version") as f:
			self.parseVersion(f)

		# Note, "podman build" and subsequent "podman push" to a Dir seems to
		# result in a manifest.json without mediaType
		with self.open("manifest.json") as f:
			mf = self.parseManifest(f, missingMediaTypeOK = True)

		img = Image(self.name, self.version)
		img.setManifest(mf, self)

		return img

	def parseVersion(self, f):
		version = None

		for l in f.readlines():
			l = l.strip()
			if not l:
				continue

			(header, value) = l.split(':', 1)
			if header != "Directory Transport Version":
				info("version file contains unknown header \"%s\"" % header)
				continue

			if version:
				raise ImageLoadError(self.spec(), "version file contains duplicate header \"%s\"" % header)

			version = value.strip()

		if version is None:
			raise ImageLoadError(self.spec(), "No Directory Transport Version header in version file")

		if version != "1.1":
			raise ImageLoadError(self.spec(), "Incompatible Directory Transport Version \"%s\" in version file" % version)

		self.transportVersion = version

	def blobExists(self, img, d):
		dstPath = self.blobFilesystemPath(d)
		return os.path.exists(dstPath)

	def blobFilesystemPath(self, d):
		name = self.digestStripPrefix(d.digest)

		filename = os.path.join(self.path, name)
		# print("blobFilesystemPath(%s) = %s" % (d.digest, filename))
		return filename

	def tryToHardlinkBlob(self, loader, d):
		srcPath = loader.blobFilesystemPath(d)
		if srcPath is None or not os.path.isfile(srcPath):
			return False

		dstPath = self.blobFilesystemPath(d)

		if os.path.exists(dstPath):
			os.remove(dstPath)

		try:
			os.link(srcPath, dstPath)
			# print("Created hard link %s -> %s" % (srcPath, dstPath))
			return True
		except:
			pass

		return False

	def openBlob(self, img, d, mode = "rb"):
		path = self.blobFilesystemPath(d)
		return open(path, mode)

	def savePreamble(self, img):
		with self.open("version", "w") as f:
			self.writeString(f, "Directory Transport Version: %s" % self.transportVersion)

	def saveManifest(self, img, mf):
		info("Writing manifest")
		with self.open("manifest.json", "w") as f:
			self.writeManifest(f, mf)

	def saveBlob(self, img, d, src):
		dst = self.openBlob(img, d, "wb")
		while True:
			b = src.read(65536)
			if not b:
				break
			dst.write(b)

		dst.flush()

	def digestStripPrefix(self, digest):
		if ':' in digest:
			digest = digest.split(':', 1)[1]
		return digest

import contextlib

class TarMemberWriter(contextlib.AbstractContextManager):
	def __init__(self, path, mode, th):
		self.f = tempfile.NamedTemporaryFile(mode = mode, delete = True)
		self.path = path
		self.th = th

	def __enter__(self):
		return self.f

	def __exit__(self, exc_type, exc_value, traceback):
		if exc_type:
			return False

		self.f.flush()

		f = open(self.f.name, mode = "rb")

		info = self.th.gettarinfo(arcname = self.path, fileobj = f)
		info.uname = 'root'
		info.gname = 'root'
		info.mode = 0o644
		info.uid = 0
		info.gid = 0

		# print("Adding %s to tar file: size %d" % (self.path, info.size))
		self.th.addfile(info, fileobj = f)

class ImageFormatOCIArchive(ImageFormat):
	def __init__(self, path, name = None):
		self.path = path
		self.name = name or os.path.basename(path)
		self.version = None

		self._th = False
		self._writing = False
		self._lastBlobSaved = None

		self.ociLayout = {
			'imageLayoutVersion': '1.0.0'
		}

	def close(self):
		self._th = False
		self._writing = False

	def spec(self):
		return "oci-archive:%s" % self.path

	def open(self, relativePath, mode = "r"):
		if self._writing and 'w' not in mode or \
		   not self._writing and 'w' in mode:
			raise ValueError("%s: file mode %s not compatible with archive open mode" % (self.__class__.__name__, mode))

		if self._writing:
			return TarMemberWriter(relativePath, mode, self._th)

		member = self._th.getmember(relativePath)
		f = self._th.extractfile(member)
		if 'b' not in mode:
			return io.TextIOWrapper(f)

		return f

	def load(self):
		import tarfile

		self.close()

		self._th = tarfile.open(self.path, mode = 'r')

		with self.open("oci-layout") as f:
			self.parseOCILayout(f)

		with self.open("index.json") as f:
			mfList = self.parseImageIndex(f, missingMediaTypeOK = True)

		desc = self.pickManifestFromIndex(mfList)

		img = Image(self.name, self.version)

		# Now load the image for that manifest
		with self.openManifest(img, desc) as f:
			mf = self.parseManifest(f, missingMediaTypeOK = True)

		img.setManifest(mf, self)

		return img

	def openManifest(self, img, d):
		return self.openBlob(img, d)

	def savePreamble(self, img):
		import tarfile

		self.close()

		self._th = tarfile.open(self.path, mode = 'w')
		self._writing = True

		with self.open("oci-layout", "w") as f:
			self.storeJSON(f, self.ociLayout)

	def parseOCILayout(self, f):
		data = self.loadJSON(f)

		version = data.get('imageLayoutVersion')
		if version != '1.0.0':
			raise ImageLoadError(self.spec(), "Unexpected imageLayoutVersion \"%s\" in oci-layout" % version)

		self.ociLayout = data

	def blobMemberPath(self, d):
		name = d.digest
		if ':' in name:
			name = name.replace(':', '/')

		return "blobs/" + name

	def openBlob(self, img, d, mode = "rb"):
		path = self.blobMemberPath(d)
		return self.open(path, mode)

	def saveManifest(self, img, mf):
		info("Writing manifest")
		index = ImageIndex()
		index.manifests.append(img.manifest)

		with self.open("index.json", "w") as f:
			self.writeImageIndex(f, index)

	def blobExists(self, img, d):
		return self._lastBlobSaved == d.digest

	def saveBlob(self, img, d, src):
		with self.openBlob(img, d, "wb") as dst:
			while True:
				b = src.read(65536)
				if not b:
					break
				dst.write(b)

			dst.flush()

		self._lastBlobSaved = d.digest

class ImageFormatFactory(object):
	defaultRegistryURL = "https://registry.opensuse.org"

	formatDict = {
		"docker": ImageFormatDockerRegistry,
		"dir": ImageFormatDir,
		"oci-archive": ImageFormatOCIArchive,
	}

	@staticmethod
	def parseImageSpec(spec):
		import re

		m = re.match("^([-a-z]+):(.*)", spec)
		if m:
			(type, name) = m.groups()
		else:
			(type, name) = ('docker', spec)

		fmt = ImageFormatFactory.getLoaderClass(type)
		return fmt(name)

	@staticmethod
	def getLoaderClass(type):
		fmt = ImageFormatFactory.formatDict.get(type)
		if fmt is None:
			raise ValueError("Unknown image type \"%s\" in \"%s\"" % (type, spec))

		return fmt

class ImageBlobCache(object):
	class CacheObject:
		def __init__(self, dirpath, filepath):
			self._directory = dirpath
			self._filename = filepath

		def open(self, mode = "rb"):
			if mode.startswith('w'):
				if not os.path.isdir(self._directory):
					os.makedirs(self._directory)
			elif not os.path.isfile(self._filename):
				return None

			return open(self._filename, mode)

		def put(self, resp):
			f = self.open("wb")
			while True:
				b = resp.read(65536)
				if not b:
					break
				f.write(b)

			f.flush()
			f.close()

	def __init__(self, path):
		self._path = path

	def createHandleFor(self, digest):
		return self.CacheObject(self._path, os.path.join(self._path, digest))


class Image(object):
	def __init__(self, name, version = None):
		self.name = name
		self.version = version
		self.manifest = None
		self.loader = None

		self._config = None
		self._layers = {}

	def spec(self):
		if not self.loader:
			raise ValueError("No external storage associated with this image")

		return self.loader.spec()

	def setManifest(self, manifest, loader = None):
		self.manifest = manifest
		self.loader = loader

		if self.version is None or self.version == 'latest':
			# Get the version from the image
			pass


	def getPotentialBaseImages(self):
		info("Checking for base images")

		config = img.getConfig()
		labels = config.config.get('Labels') or {}

		baseImages = set()
		for (label, value) in labels.items():
			if (label.startswith('org.opensuse.') or label.startswith('com.suse.')) and \
			   label.endswith('.reference'):
				info("  This image is based on %s" % value)
				baseImages.add(value)

		return baseImages

	def getConfig(self):
		if not self._config:
			assert(self.loader)
			assert(self.manifest)
			assert(self.manifest.configDescriptor)

			self._config = self.loader.loadConfig(self, self.manifest.configDescriptor)

		return self._config

	def openLayerBlob(self, d, uncompress = False):
		assert(d in self.manifest.layers)

		f = self.loader.openBlob(self, d, mode = "rb")
		if uncompress:
			comp = d._parsedMediaType.compression
			if comp is None:
				pass
			elif comp == "gzip":
				import gzip
				f = gzip.GzipFile(mode = "rb", fileobj = f)
			else:
				raise ValueError("Compression mode \"%s\" currently not supported" % comp)

		return f

class ImageFactory:
	def __init__(self, architecture = "amd64"):
		self._architecture = architecture
		self._cache = ".cache"
		self._keystore = None

	def setKeystore(self, keystore):
		self._keystore = keystore

	def getImageStorage(self, spec):
		store = ImageFormatFactory.parseImageSpec(spec)

		store.setArchitecture(self._architecture)
		store.setCacheDir(self._cache)
		store.setKeystore(self._keystore)
		return store

	def load(self, spec):
		loader = self.getImageStorage(spec)
		return loader.load()

class MediaType:
	class VendorDocker:
		name = 'docker'
		separator = '.'

		mimeTypeImageIndex = 'application/vnd.docker.distribution.manifest.list.v2+json'
		mimeTypeManifest = 'application/vnd.docker.distribution.manifest.v2+json'
		mimeTypeConfig = 'application/vnd.docker.container.image.v1+json'
		mimeTypeLayer = 'application/vnd.docker.image.rootfs.diff.tar'
		mimeTypeLayerExternal = 'application/vnd.docker.image.rootfs.foreign.diff.tar'

	class VendorOCI:
		name = 'oci'
		separator = '+'

		mimeTypeImageIndex = 'application/vnd.oci.image.index.v1+json'
		mimeTypeManifest = 'application/vnd.oci.image.manifest.v1+json'
		mimeTypeConfig = 'application/vnd.oci.image.config.v1+json'
		mimeTypeLayer = 'application/vnd.oci.image.layer.v1.tar'
		mimeTypeLayerExternal = 'application/vnd.oci.image.layer.nondistributable.v1.tar'

	compressionAlgorithms = (
		'gzip',
		'zstd',
	)

	def __init__(self, mt):
		self.baseMIMEType = mt
		self.compression = None
		self.vendor = None

		if mt.startswith('application/vnd.oci.'):
			# OCI media types end with +gzip
			self.vendor = MediaType.VendorOCI
		elif mt.startswith('application/vnd.docker.'):
			# Docker media types end with .gzip
			self.vendor = MediaType.VendorDocker

		if self.vendor:
			sepa = self.vendor.separator
			for compression in self.compressionAlgorithms:
				if mt.endswith(sepa + compression):
					n = len(compression) + 1
					self.baseMIMEType = mt[:-n]
					self.compression = compression
					break

	def __repr__(self):
		if not self.compression:
			return self.baseMIMEType

		return "".join(self.baseMIMEType, self.vendor.separator, self.compression)

	@staticmethod
	def mimeTypesFor(what):
		result = []
		for vendor in (MediaType.VendorDocker, MediaType.VendorOCI):
			result.append(getattr(vendor, 'mimeType' + what))

		return result

	class Validator:
		def __init__(self, defaultHandler = None):
			self._mediaTypeHandlers = {}
			self._defaultHandler = defaultHandler

		def acceptMediaTypes(self, kind, handler):
			for mt in MediaType.mimeTypesFor(kind):
				self._mediaTypeHandlers[mt] = handler

		def validateJSON(self, data):
			mediaType = data.get('mediaType')
			if not mediaType:
				if self._defaultHandler:
					return self._defaultHandler
			elif mediaType in self._mediaTypeHandlers:
				return self._mediaTypeHandlers[mediaType]

			print("Problem with JSON received from server")
			data = json.dumps(data, cls = JSONObjectEncoder, indent = "\t")
			print(data)

			compatibleTypes = ", ".join(self._mediaTypeHandlers.keys())
			raise ValueError(f"JSON contains unexpected mediaType {mediaType}; expected one of {compatibleTypes}")

	@staticmethod
	def indexValidator(missingMediaTypeOK = False):
		return MediaType.Validator(MediaType.mimeTypesFor('ImageIndex'), missingMediaTypeOK)

	@staticmethod
	def manifestValidator(missingMediaTypeOK = False):
		return MediaType.Validator(MediaType.mimeTypesFor('Manifest'), missingMediaTypeOK)

	def isImageIndex(self):
		if not self.vendor:
			return False

		return self.baseMIMEType == self.vendor.mimeTypeImageIndex

	def isManifest(self):
		if not self.vendor:
			return False

		return self.baseMIMEType == self.vendor.mimeTypeManifest

	def isImageLayer(self):
		if not self.vendor:
			return False

		return self.baseMIMEType == self.vendor.mimeTypeLayer

	def isExternalReference(self):
		return self.baseMIMEType == self.vendor.mimeTypeLayerExternal

	def makeExternalReference(self):
		assert(self.vendor)

		if not self.isImageLayer():
			raise ValueError("Cannot convert mime type \"%s\" to an external reference layer" % self.baseMIMEType)

		self.baseMIMEType = self.vendor.mimeTypeLayerExternal

	def changeVendor(self, vendor):
		mimeTypeAttr = self._identifyMIMEType()

		newType = getattr(vendor, mimeTypeAttr)
		# print("vendor %s -> %s; mimeType %s -> %s" % (self.vendor.name, vendor.name, self.baseMIMEType, newType))
		self.baseMIMEType = newType
		self.vendor = vendor

	def _identifyMIMEType(self):
		for attr in dir(self.vendor):
			mt = getattr(self.vendor, attr)
			if mt == self.baseMIMEType:
				return attr

		return None

class ContainerStatus(object):
	def __init__(self, data):
		self._data = data

	def _get(self, key, defaultValue = None):
		if not self._data:
			return defaultValue
		return self._data.get(key) or defaultValue

	def __str__(self):
		return f"container {self.id} state {self.state}"

	@property
	def id(self):
		return self._get('Id')

	@property
	def labels(self):
		return self._get('Labels', {})

	@property
	def imageVersion(self):
		return self.labels.get('org.opencontainers.image.version')

	@property
	def names(self):
		return self._get('Names', [])

	@property
	def imageName(self):
		return self._get('Image')

	@property
	def imageId(self):
		return self._get('ImageId')

	@property
	def state(self):
		return self._get('State')

class LayerMap(object):
	class LayerInfo:
		def __init__(self, generation, d):
			self.generation = generation
			self.descriptor = d

	def __init__(self):
		self._dict = {}

	def isEmpty(self):
		return len(self._dict) == 0

	def get(self, digest):
		return self._dict.get(digest)

	def add(self, generation, d, reference):
		digest = d.digest
		l = self._dict.get(digest)
		if l is not None:
			assert(generation < l.generation)

		l = self.LayerInfo(generation, d)
		l.reference = reference
		self._dict[digest] = l

		return l

	def addImage(self, spec):
		ti = logger.incrementIndent()

		try:
			img = imageFactory.load(spec)
		except ImageLoadError as e:
			info("%s: %s" % (e.image, e.reason))
			return

		for d in img.manifest.layers:
			info("Image %s layer %s (%s)" % (img.spec(), d.mediaType, d.digest))
			layerMap.addReferencedLayer(img, d)

	def addReferencedLayer(self, img, d):
		ti = logger.incrementIndent()

		# For now, we try to reference layers from the image
		# with the longest match. If our image has several layers
		# that can be externalized, this helps to make all these
		# external layers reference the same image.
		generation = len(img.manifest.layers)

		mediaType = MediaType(d.mediaType)
		if not d.isImageLayer():
			info("Image %s has layer of unknown type %s" % (img.spec(), d.mediaType))
			return

		l = self.get(d.digest)
		if l and l.generation <= generation:
			info("Layer already mapped")
			return

		refDest = d.asExternalReference(img)
		info("Making external ref with mediaType %s" % refDest._parsedMediaType)

		self.add(generation, d, refDest)

		if d.isCompressed():
			info("Layer is compressed, hashing uncompressed data")
			try:
				newDesc = d.asUncompressed(img)
			except ImageLoadError as e:
				info("%s: %s" % (e.image, e.reason))
				return

			info("  %s (%s)" % (newDesc.mediaType, newDesc.digest))
			self.add(generation, newDesc, refDest)

	def getReference(self, d):
		l = self.get(d.digest)
		if l is None:
			return None

		if False:
			if d.mediaType != l.descriptor.mediaType:
				print("WARN: incompatible media types %s -> %s" % (d.mediaType, l.descriptor.mediaType))

		ref = l.reference

		# If the rest of the manifest uses eg OCI mime types, make sure our external references
		# have the same vendor prefix.
		vendor = d.vendor()
		if not ref.sameVendor(vendor):
			return ref.asVendor(vendor)

		return ref
