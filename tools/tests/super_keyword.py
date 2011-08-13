#!/usr/bin/python

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

display = {}

class SuperclassTestCase(EZIOTestCase):
    project_name = 'super_keyword'
    target_template = 'simple_superclass'

    def get_display(self):
        return display

    def test(self):
        super(SuperclassTestCase, self).test()
        # experimental control:
        assert_equal(self.lines, ['simple_superclass::first', 'simple_superclass::second'])

class SubclassTestCase(EZIOTestCase):
    project_name = 'super_keyword'
    target_template = 'simple_subclass'

    def get_display(self):
        return display

    def test(self):
        super(SubclassTestCase, self).test()

        assert_equal(self.lines,
                ['simple_subclass::first',
                 'simple_subclass::second',
                 # test that we dispatched correctly to the superclass method
                 'simple_superclass::second',
                ])

if __name__ == '__main__':
    testify.run()
