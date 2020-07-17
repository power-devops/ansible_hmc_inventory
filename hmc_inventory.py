#!/usr/bin/python3.7

from __future__ import print_function

import argparse
import json
import os
import ssl
import sys
import xml.etree.ElementTree as ET

try:
  import requests
except ModuleNotFoundError:
  print("requests module is required. Hint: sudo yum -y install python3-requests", file=sys.stderr)
  sys.exit(1)

from collections import defaultdict

try:
  from ansible.module_utils.six.moves import configparser as ConfigParser
except ModuleNotFoundError:
  print("ansible is required. Hint: sudo yum -y install ansible", file=sys.stderr)
  sys.exit(1)

try:
  from jinja2 import Template
except ModuleNotFoundError:
  print("jinja2 is required. Hint: sudo yum -y install python3-jinja2", file=sys.stderr)
  sys.exit(1)

import urllib3
urllib3.disable_warnings()

def xml_parse(xml):
  it = ET.fromstring(xml)
  for el in it:
    prefix, has_namespace, postfix = el.tag.rpartition('}')
    if has_namespace:
      el.tag = postfix
  return it

class PowerHMC(object):
  """HMC connection object"""
  def __init__(self, url, user, password, verify_ssl):
    if url.startswith("http"):
      self.url = url
    else:
      self.url = "https://" + url + ":12443/"
    self.user = user
    self.password = password
    self.verify_ssl = verify_ssl
    self.session = ""
    self.logged_in = False
    self.logon()

  def logon(self):
    """establishes an HMC connection and obtains session token"""
    if self.logged_in:
      return True
    tpl = Template("""
  <LogonRequest xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/" schemaVersion="V1_0">
    <Metadata>
      <Atom/>
    </Metadata>
    <UserID kb="CUR" kxe="false">{{user}}</UserID>
    <Password kb="CUR" kxe="false">{{password}}</Password>
  </LogonRequest>
    """)
    authreq = tpl.render(user = self.user, password = self.password)
    headers = {
      "Content-Type": "application/vnd.ibm.powervm.web+xml; type=LogonRequest",
      "Accept": "application/vnd.ibm.powervm.web+xml; type=LogonResponse",
      "X-Audit-Memento": "Ansible HMC inventory",
    }
    try:
      r = requests.put(self.url + "rest/api/web/Logon", headers = headers, data = authreq, verify = self.verify_ssl)
    except (ssl.SSLCertVerificationError, requests.exceptions.SSLError) as e:
      print("SSL exception: %s" % e, file=sys.stderr)
      sys.exit(1)
    if r.status_code == requests.codes.ok:
      self.session = xml_parse(r.text).find("X-API-Session").text.strip()
      if self.session != "":
        self.logged_in = True
        return True
    return False

  def logoff(self):
    """disconnect from HMC"""
    if not self.logged_in:
      return True
    if self.session == "":
      self.logged_in = False
      return True
    headers = {
      "Content-Type": "application/vnd.ibm.powervm.web+xml; type=LogonRequest",
      "Accept": "application/vnd.ibm.powervm.web+xml; type=LogonResponse",
      "X-Audit-Memento": "Ansible HMC inventory",
      "X-API-Session": self.session,
    }
    try:
      r = requests.delete(self.url + "rest/api/web/Logon", headers = headers, verify = self.verify_ssl)
    except (ssl.SSLCertVerificationError, requests.exceptions.SSLError) as e:
      print("SSL exception: %s" % e, file=sys.stderr)
      sys.exit(1)
    if r.status_code == requests.codes.ok:
      self.session = ""
      self.logged_in = False
      return True
    return False

  def get(self, api, content):
    """make a GET request to HMC"""
    if not self.logged_in:
      return ("", False)
    if self.session == "":
      return ("", False)
    if api.startswith("http"):
      url = api
    else:
      url = self.url + api
    headers = {
      "Content-Type": "application/vnd.ibm.powervm.uom+xml; type="+content,
      "Accept": "application/vnd.ibm.powervm.uom+xml; type="+content,
      "X-Audit-Memento": "Ansible HMC inventory",
      "X-API-Session": self.session,
    }
    try:
      r = requests.get(url, headers = headers, verify = self.verify_ssl)
    except (ssl.SSLCertVerificationError, requests.exceptions.SSLError) as e:
      print("SSL exception: %s" % e, file=sys.stderr)
      sys.exit(1)
    if r.status_code == requests.codes.ok:
      return (r.text, True)
    return ("", False)

  def logical_partitions(self):
    """returns a list of logical partitions managed by the HMC"""
    out, err = self.get("rest/api/uom/LogicalPartition", "LogicalPartition")
    if err == False:
      return ([], False)
    lpars = []
    xml = xml_parse(out)
    for child in xml:
      if child.tag == "entry":
        for entry in child:
          if entry.tag == "{http://www.w3.org/2005/Atom}content":
            for content in entry:
              if content.tag == "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}LogicalPartition":
                for lpar in content:
                  if lpar.tag == "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}PartitionName":
                    lpars.append(lpar.text)
    lpars.sort()
    return (lpars, True)

class HmcInventory(object):
  def __init__(self):
    self.inventory = defaultdict(list)
    self.cache = dict()
    self.params = dict()
    self.facts = dict()
    self.session = None
    self.config_paths = [
      "/etc/ansible/hmcinv.ini",
      os.path.expanduser("~") + '/.hmcinv.ini',
      os.path.dirname(os.path.realpath(__file__)) + '/hmcinv.ini',
    ]
    env_value = os.environ.get('HMCINV_INI_PATH')
    if env_value is not None:
      self.config_paths.append(os.path.expanduser(os.path.expandvars(env_value)))

  def read_cli_args(self):
    """Read the command line args passed to the script."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--list', action = 'store_true', default=True, help='List all LPARs (default: True)')
    parser.add_argument('--refresh-cache', action='store_true', default=False,
                         help='Force refresh of cache by making API requests to HMC (default: False - use cache files)')
    self.args = parser.parse_args()

  def read_settings(self):
    """Read settings from hmcinv.ini file"""
    config = ConfigParser.ConfigParser()
    config.read(self.config_paths)
    try:
      self.hmc_url = config.get('hmc', 'url')
      self.hmc_user = config.get('hmc', 'user')
      self.hmc_pw = config.get('hmc', 'password', raw=True)
      self.hmc_ssl_verify = config.getboolean('hmc', 'ssl_verify')
    except (ConfigParser.NoOptionError, ConfigParser.NoSectionError) as e:
      print("Error parsing configuration: %s" % e, file=sys.stderr)
      return False

    try:
      cache_path = os.path.expanduser(config.get('cache', 'path'))
    except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
      cache_path = '.'
    (script, ext) = os.path.splitext(os.path.basename(__file__))
    self.cache_path_cache = cache_path + "/%s.cache" % script
    self.cache_path_inventory = cache_path + "/%s.index" % script
    self.cache_path_params = cache_path + "/%s.params" % script
    self.cache_path_facts = cache_path + "/%s.facts" % script

    try:
      self.cache_max_age = config.getint('cache', 'max_age')
    except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
      self.cache_max_age = 60

    return True

  def write_to_cache(self):
    """Write cache in JSON format to a file"""
    json_data = json.dumps(self.inventory, sort_keys=True, indent=2)
    cache = open(self.cache_path_inventory, 'w')
    cache.write(json_data)
    cache.close()

  def update_cache(self):
    """Make calls to the HMC and updates the cache"""
    h = PowerHMC(self.hmc_url, self.hmc_user, self.hmc_pw, self.hmc_ssl_verify)
    (lpars, err) = h.logical_partitions()
    h.logoff()
    if err == False:
      self.inventory = defaultdict(list)
      self.write_to_cache()
      return
    for lpar in lpars:
      self.inventory['all'].append(lpar)
    self.write_to_cache()

  def is_cache_valid(self):
    """Determines if the cache is still valid"""
    if os.path.isfile(self.cache_path_cache):
      mod_time = os.path.getmtime(self.cache_path_cache)
      current_time = time()
      if (mod_time + self.cache_max_age) > current_time:
        if (os.path.isfile(self.cache_path_inventory) and
          os.path.isfile(self.cache_path_params) and
            os.path.isfile(self.cache_path_facts)):
          return True
    return False

  def load_inventory_from_cache(self):
    """Read the index from the cache file sets self.index"""
    with open(self.cache_path_inventory, 'r') as fp:
      self.inventory = json.load(fp)

  def get_inventory(self):
    if self.args.refresh_cache or not self.is_cache_valid():
      self.update_cache()
    else:
      self.load_inventory_from_cache()

  def _print_data(self):
    data_to_print = ""
    self.inventory['_meta'] = {'hostvars': {}}
    data_to_print += json.dumps(self.inventory, sort_keys=True, indent=2)
    print(data_to_print)

  def run(self):
    if not self.read_settings():
      return False
    self.read_cli_args()
    self.get_inventory()
    self._print_data()
    return True

if __name__ == '__main__':
    sys.exit(not HmcInventory().run())
