# hmc_inventory

This is a small script to build a dynamic inventory from a Power HMC for Ansible.

## Prereqs

* Python 3
* python3-requests
* python3-jinja2
* Ansible
* HMC v8 or newer (Rest API support)

The script was written and tested on AIX 7.2 TL4 SP1 and HMC V9R1M930 with Ansible 2.9.4.

## Using

Create hmcinv.ini file in the current directory or in /etc/ansible. A self-explanatory example is in the repository.

```
# ansible all -i hmc_inventory.py -m ping
```
