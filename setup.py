#!/usr/bin/env python

from distutils.core import setup

setup(name='AutoCertKit',
      version='1.2.0',
      author='Citrix System Inc.',
      url='http://github.com/xenserver/auto-cert-kit',
      packages=['autocertkit', 'XenAPI', 'acktools', 'acktools.net'],
      )
