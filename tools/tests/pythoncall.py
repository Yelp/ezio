#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase

SHIBBOLETH = "thisisthereturnvalueofthebarmethod"

class ValueStore(object):
    def bar(self):
        return SHIBBOLETH

VALUE_STORE = ValueStore()

display = {'baz': VALUE_STORE}

class TestCase(EZIOTestCase):

    target_template = 'pythoncall'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [VALUE_STORE, SHIBBOLETH]

    def test(self):
        super(TestCase, self).test()

        assert SHIBBOLETH in self.result

if __name__ == '__main__':
    testify.run()
