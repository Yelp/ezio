#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase

display = {}

class TestCase(EZIOTestCase):

    target_template = 'missing_name'

    def get_display(self):
        return display

    # TODO define a behavior for this; for now, just assert that we don't crash

if __name__ == '__main__':
    testify.run()
