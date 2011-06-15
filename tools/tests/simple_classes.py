#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase
from tools.tests.simple import display

class SuperclassTestCase(EZIOTestCase):
    project_name = 'simple_classes'
    target_template = 'simple_superclass'

    def get_display(self):
        return display

    def test(self):
        super(SuperclassTestCase, self).test()

        assert 'superclass' in self.result
        assert 'subclass' not in self.result

class SubclassTestCase(EZIOTestCase):
    project_name = 'simple_classes'
    target_template = 'simple_subclass'

    def get_display(self):
        return display

    def test(self):
        super(SubclassTestCase, self).test()

        assert 'superclass' not in self.result
        assert 'subclass' in self.result
        # this is part of the main body of the template,
        # which should be dead code in a subclass
        assert 'unreachable_statement' not in self.result

if __name__ == '__main__':
    testify.run()
