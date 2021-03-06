"""Fabric file."""

import base64
import os
import json
import urllib2

from contextlib import contextmanager

from fabric.api import local, lcd, abort
from fabric.decorators import task


BUILD_DIRS = (
    "dist",
    "build",
    "demo/build",
    "sphinx_bootstrap_theme.egg-info",
)

SDIST_RST_FILES = (
    "README.rst",
    "HISTORY.rst",
)
SDIST_TXT_FILES = [os.path.splitext(x)[0] + ".txt" for x in SDIST_RST_FILES]


###############################################################################
# Misc.
###############################################################################
@task
def clean():
    """Clean build files."""
    for build_dir in list(BUILD_DIRS):
        local("rm -rf %s" % build_dir)


@task
def demo():
    """Clean build files."""
    with lcd("demo"):
        local("make html")


###############################################################################
# PyPI
###############################################################################
@contextmanager
def _dist_wrapper():
    """Add temporary distribution build files (and then clean up)."""
    try:
        # Copy select *.rst files to *.txt for build.
        for rst_file, txt_file in zip(SDIST_RST_FILES, SDIST_TXT_FILES):
            local("cp %s %s" % (rst_file, txt_file))

        # Perform action.
        yield
    finally:
        # Clean up temp *.txt files.
        for rst_file in SDIST_TXT_FILES:
            local("rm -f %s" % rst_file, capture=False)


@task
def sdist():
    """Package into distribution."""
    with _dist_wrapper():
        local("python setup.py sdist", capture=False)


@task
def pypi_register():
    """Register and prep user for PyPi upload.

    .. note:: May need to weak ~/.pypirc file per issue:
        http://stackoverflow.com/questions/1569315
    """
    with _dist_wrapper():
        local("python setup.py register", capture=False)


@task
def pypi_upload():
    """Upload package."""
    with _dist_wrapper():
        local("python setup.py sdist upload", capture=False)


###############################################################################
# GitHub
###############################################################################
class Request(urllib2.Request):
    """Request with method support."""

    def __init__(self, *args, **kwargs):
        """Initializer."""
        self._method = kwargs.pop('method', "GET")
        urllib2.Request.__init__(self, *args, **kwargs)

    def get_method(self):
        """Method."""
        return self._method


class GitHub(object):
    """GitHub API wrapper."""

    def __init__(self):
        """Initializer."""
        self.user = self.config("user")
        self.password = self.config("password")
        self.token = self.config("token")
        self.repo = "sphinx-bootstrap-theme"
        self.api_base = "https://api.github.com"

    @classmethod
    def _add_headers(cls, req, headers=None):
        """Format headers."""
        if headers:
            if isinstance(headers, dict):
                headers = headers.iteritems()

            for key, val in headers:
                req.add_header(key, val)

    @classmethod
    def config(cls, key):
        """Get a .gitconfig GH value."""
        val = local("git config github.%s" % key, capture=True).strip()
        return val if val else None

    def api_op(self, path, method="GET", headers=None, data=None):
        """Perform a GitHub API request and decode to JSON."""
        # Params: URL, data, auth string.
        url_path = self.api_base
        if path:
            url_path = "/".join((self.api_base, path.lstrip("/")))
        auth_str = base64.encodestring(
            "%s:%s" % (self.user, self.password))[:-1]

        req = Request(url_path, method=method, data=data)
        self._add_headers(req, headers)
        req.add_header("Authorization", "Basic %s" % auth_str)

        results = urllib2.urlopen(req).read()
        return json.loads(results) if results else {}

    def downloads(self):
        """Retrieve current GitHub downloads."""
        return self.api_op("repos/%s/%s/downloads" % (self.user, self.repo))

    def downloads_del(self, dl_obj):
        """Delete a download file."""
        return self.api_op(
            "repos/%s/%s/downloads/%s" % (self.user, self.repo, dl_obj['id']),
            method="DELETE",
        )

    def downloads_put(self, file_name, suffix, desc=None):
        """Upload a download file."""

        if not desc:
            desc = "Pre-packaged sphinx theme for %s." % suffix

        # Part 1: Create the resource.
        file_size = os.path.getsize(file_name)
        meta = self.api_op(
            "repos/%s/%s/downloads" % (self.user, self.repo),
            method="POST",
            headers={
                'Content-Type': "application/json",
            },
            data=json.dumps({
                "name": os.path.basename(file_name),
                "size": file_size,
                "description": desc,
                #"content_type": "text/plain" (Optional)
            }),
        )

        meta.update({
            'file_path': file_name,
        })

        # Part 2: Upload file to s3 (using shelled curl).
        local("curl "
              "-F \"key=%(path)s\" "
              "-F \"acl=%(acl)s\" "
              "-F \"success_action_status=201\" "
              "-F \"Filename=%(name)s\" "
              "-F \"AWSAccessKeyId=%(accesskeyid)s\" "
              "-F \"Policy=%(policy)s\" "
              "-F \"Signature=%(signature)s\" "
              "-F \"Content-Type=%(mime_type)s\" "
              "-F \"file=@%(file_path)s\" "
              "https://github.s3.amazonaws.com/" % meta)

        return meta


def get_suffix(tag=False):
    """Get build suffix.

    @param tag  Use git tag instead of hash?
    """
    suffix_cmd = "git describe --always --tag" if tag in (True, "True") else \
                 "git rev-parse HEAD"
    return local(suffix_cmd, capture=True).strip()


@task
def gh_bundle(tag=False):
    """Create zip file upload bundles.

    @param tag  Use git tag instead of hash?
    """
    suffix = get_suffix(tag)

    print("Cleaning old build files.")
    clean()

    local("mkdir -p build")

    print("Bundling new files.")
    with lcd("sphinx_bootstrap_theme/bootstrap"):
        local("zip -r ../../build/bootstrap.zip .")

    with lcd("build"):
        local("cp bootstrap.zip bootstrap-%s.zip" % suffix)

        print("Verifying contents.")
        local("unzip -l bootstrap.zip")


@task
def gh_downloads():
    """Verify GitHub downloads."""
    print("Downloads:")
    for download in GitHub().downloads():
        print("%(created_at)s: %(name)s (%(id)s)" % download)


@task
def gh_upload(tag=False):
    """Upload new zip files.

    @param tag  Use git tag instead of hash?
    """
    suffix = get_suffix(tag)
    base_zip = "build/bootstrap.zip"
    base_file = os.path.basename(base_zip)
    suffix_zip = "build/bootstrap-%s.zip" % suffix
    suffix_file = os.path.basename(suffix_zip)

    if not (os.path.exists(base_zip) and os.path.exists(suffix_zip)):
        abort("Did not find current zip files. Please create.")

    # Check if existing downloads
    github = GitHub()
    dl_dict = dict((x['name'], x) for x in github.downloads())
    dl_suffix = dl_dict.get(suffix_file)
    dl_base = dl_dict.get(base_file)

    if dl_suffix is not None:
        print("Found suffixed zip file already. Skipping")
        return

    if dl_base is not None:
        print("Removing current base zip file.")
        result = github.downloads_del(dl_base)
        print("Result: %s" % json.dumps(result, indent=2))

    print("Upload new base zip file.")
    result = github.downloads_put(base_zip, suffix)
    print("\nResult: %s" % json.dumps(result, indent=2))

    print("Upload new suffixed zip file.")
    result = github.downloads_put(suffix_zip, suffix)
    print("\nResult: %s" % json.dumps(result, indent=2))
