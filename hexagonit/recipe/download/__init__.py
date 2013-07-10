from fnmatch import fnmatch
from zc.buildout.download import Download

import sys
import logging
import os.path
import shutil
import tempfile
import zc.buildout
import tarfile
import zipfile
from distutils.errors import DistutilsError
from pkg_resources import ensure_directory

if sys.version_info[0] > 2:
    import urllib.parse as urlparse
else:
    import urlparse

TRUE_VALUES = ('yes', 'true', '1', 'on')


class UnrecognizedFormat(DistutilsError):
    """Couldn't recognize the archive type"""


class Recipe(object):
    """Recipe for downloading packages from the net and extracting them on
    the filesystem.
    """

    def __init__(self, buildout, name, options):
        self.options = options
        self.buildout = buildout
        self.name = name
        self.log = logging.getLogger(self.name)

        options.setdefault(
            'destination', os.path.join(
            buildout['buildout']['parts-directory'],
            self.name))
        options['location'] = options['destination']
        options.setdefault('strip-top-level-dir', 'false')
        options.setdefault('ignore-existing', 'false')
        options.setdefault('download-only', 'false')
        options.setdefault('hash-name', 'true')
        options.setdefault('on-update', 'false')
        options['filename'] = options.get('filename', '').strip()

        if options.get('mode'):
            options['mode'] = options['mode'].strip()

        # buildout -vv (or more) will trigger verbose mode
        self.verbose = int(buildout['buildout'].get('verbosity', 0)) >= 20
        self.excludes = [x.strip() for x in options.get('excludes', '').strip().splitlines() if x.strip()]

    def progress_filter(self, src, dst):
        """Filter out contents from the extracted package."""
        for exclude in self.excludes:
            if fnmatch(src, exclude):
                if self.verbose:
                    self.log.debug("Excluding %s" % src.rstrip('/'))
                self.excluded_count = self.excluded_count + 1
                return
        return dst

    def update(self):
        if self.options['on-update'].strip().lower() in TRUE_VALUES:
            self.install()

    def calculate_base(self, extract_dir):
        """
        recipe authors inheriting from this recipe can override this method to set a different base directory.
        """
        # Move the contents of the package in to the correct destination
        top_level_contents = os.listdir(extract_dir)
        if self.options['strip-top-level-dir'].strip().lower() in TRUE_VALUES:
            if len(top_level_contents) != 1:
                self.log.error('Unable to strip top level directory because there are more '
                               'than one element in the root of the package.')
                raise zc.buildout.UserError('Invalid package contents')
            base = os.path.join(extract_dir, top_level_contents[0])
        else:
            base = extract_dir
        return base

    def filename(self):
        if self.options['filename']:
            # Use an explicit filename from the section configuration
            filename = self.options['filename']
        else:
            # Use the original filename of the downloaded file regardless
            # whether download filename hashing is enabled.
            # See http://github.com/hexagonit/hexagonit.recipe.download/issues#issue/2
            filename = os.path.basename(urlparse.urlparse(self.options['url'])[2])
        return filename

    def is_tarfile(self, filename):
        try:
            tarfile.open(filename)
        except tarfile.TarError:
            return False
        return True

    def unpack_directory(self, filename, extract_dir):
        """"Unpack" a directory, using the same interface as for archives

        Raises ``UnrecognizedFormat`` if `filename` is not a directory
        """
        if not os.path.isdir(filename):
            raise UnrecognizedFormat("%s is not a directory" % (filename,))

        paths = {filename: ('', extract_dir)}
        for base, dirs, files in os.walk(filename):
            src, dst = paths[base]
            for d in dirs:
                paths[os.path.join(base, d)] = src + d + '/', os.path.join(dst, d)
            for f in files:
                #name = src + f
                target = os.path.join(dst, f)
                target = self.progress_filter(src + f, target)
                if not target:
                    continue    # skip non-files
                ensure_directory(target)
                f = os.path.join(base, f)
                shutil.copyfile(f, target)
                shutil.copystat(f, target)

    def unpack_zipfile(self, filename, extract_dir):
        """Unpack zip `filename` to `extract_dir`"""

        z = zipfile.ZipFile(filename)
        try:
            for info in z.infolist():
                name = info.filename

                # don't extract absolute paths or ones with .. in them
                if name.startswith('/') or '..' in name.split('/'):
                    continue

                target = os.path.join(extract_dir, *name.split('/'))
                target = self.progress_filter(name, target)
                if not target:
                    continue
                if name.endswith('/'):
                    # directory
                    ensure_directory(target)
                else:
                    # file
                    ensure_directory(target)
                    data = z.read(info.filename)
                    f = open(target, 'wb')
                    try:
                        f.write(data)
                    finally:
                        f.close()
                        del data
        finally:
            z.close()

    def unpack_tarfile(self, filename, extract_dir):
        """Unpack tar/tar.gz/tar.bz2 `filename` to `extract_dir`"""

        cwd = os.getcwd()
        os.chdir(extract_dir)

        try:
            tarobj = tarfile.open(filename)
            for member in tarobj:
                name = member.name
                # don't extract absolute paths or ones with .. in them
                if not name.startswith('/') and '..' not in name.split('/'):
                    tarobj.extract(member)
        finally:
            tarobj.close()
            os.chdir(cwd)

    def install(self):

        destination = self.options.get('destination')
        download = Download(self.buildout['buildout'], hash_name=self.options['hash-name'].strip() in TRUE_VALUES)
        path, is_temp = download(self.options['url'], md5sum=self.options.get('md5sum'))

        parts = []

        try:
            # Create destination directory
            if not os.path.isdir(destination):
                os.makedirs(destination)
                parts.append(destination)

            download_only = self.options['download-only'].strip().lower() in TRUE_VALUES
            if download_only:
                filename = self.filename()

                # Copy the file to destination without extraction
                target_path = os.path.join(destination, filename)
                shutil.copy(path, target_path)
                if self.options.get('mode'):
                    os.chmod(target_path, int(self.options['mode'], 8))
                if not destination in parts:
                    parts.append(target_path)
            else:
                # Extract the package
                extract_dir = tempfile.mkdtemp("buildout-" + self.name)
                self.excluded_count = 0
                try:
                    if os.path.isdir(path):
                        self.unpack_directory(path, extract_dir)
                    elif zipfile.is_zipfile(path):
                        self.unpack_zipfile(path, extract_dir)
                    elif self.is_tarfile(path):
                        self.unpack_tarfile(path, extract_dir)
                    else:
                        self.log.error("Unknown compression type: %s" % path)
                        raise zc.buildout.UserError('Package extraction error')

                    if self.excluded_count > 0:
                        self.log.info("Excluding %s file(s) matching the exclusion pattern." % self.excluded_count)
                    base = self.calculate_base(extract_dir)

                    if not os.path.isdir(destination):
                        os.makedirs(destination)
                        parts.append(destination)

                    self.log.info('Extracting package to %s' % destination)

                    ignore_existing = self.options['ignore-existing'].strip().lower() in TRUE_VALUES
                    for filename in os.listdir(base):
                        dest = os.path.join(destination, filename)
                        if os.path.exists(dest):
                            if ignore_existing:
                                self.log.info('Ignoring existing target: %s' % dest)
                            else:
                                self.log.error(
                                    'Target %s already exists. Either remove it or set '
                                    '``ignore-existing = true`` in your buildout.cfg to ignore existing '
                                    'files and directories.', dest)
                                raise zc.buildout.UserError('File or directory already exists.')
                        else:
                            # Only add the file/directory to the list of installed
                            # parts if it does not already exist. This way it does
                            # not get accidentally removed when uninstalling.
                            parts.append(dest)

                        shutil.move(os.path.join(base, filename), dest)
                finally:
                    shutil.rmtree(extract_dir)

        finally:
            if is_temp:
                os.unlink(path)

        return parts
