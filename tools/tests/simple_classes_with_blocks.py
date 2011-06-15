#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase
from tools.tests.nested_blocks import display

class SuperclassTestCase(EZIOTestCase):
    project_name = 'simple_classes_with_blocks'
    target_template = 'nested_blocks_superclass'

    def get_display(self):
        return display

    def test(self):
        super(SuperclassTestCase, self).test()

        assert 'before_the_block' in self.result
        assert 'outer_block' in self.result
        assert 'inner_block' in self.result
        assert 'only_in_subclass' not in self.result
        assert 'unreachable_statement' not in self.result

class SubclassTestCase(EZIOTestCase):
    project_name = 'simple_classes_with_blocks'
    target_template = 'nested_blocks_subclass'

    def get_display(self):
        return display

    def test(self):
        super(SubclassTestCase, self).test()

        assert 'before_the_block' in self.result
        # subclass does not explicitly implement the outer block;
        # this is testing that it gets called from the superclass's main
        # method and then dispatches to the superclass implementation
        assert 'outer_block' in self.result
        # subclass reimplements the inner block and does
        # s/inner_block/only_in_subclass/; this tests that the superclass's
        # outer block implementation correctly dispatches to the subclass's
        # inner block implementation
        assert 'inner_block' not in self.result
        assert 'only_in_subclass' in self.result
        # and this tests, as usual, that main-body elements of the subclass
        # are ignored:
        assert 'unreachable_statement' not in self.result

if __name__ == '__main__':
    testify.run()
