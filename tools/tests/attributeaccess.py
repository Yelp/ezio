#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase

SHIBBOLETH = "ATTRIBUTE_ACCESS_SHIBBOLETH"

class ValueStore(object):
    def __init__(self):
        self.bar = SHIBBOLETH

display = {'baz': ValueStore()}

class TestCase(EZIOTestCase):

    target_template = 'attributeaccess'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [display, display.keys()[0], display['baz'], SHIBBOLETH]

    def test(self):
        super(TestCase, self).test()
        assert SHIBBOLETH in self.result

if __name__ == '__main__':
    testify.run()
