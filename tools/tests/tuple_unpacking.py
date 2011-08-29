#!/usr/bin/python
# -*- coding: utf-8 -*-

import os

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

first_sequence = [('a', 'b'), ('c', 'd')]
second_sequence = [('w', ('x', 'y'), 'z'), ('a', ('b', 'c'), 'd')]
bar = 'thisisthebarvalue'
third_sequence = [('i', 'j'), ('k', 'l')]
fourth_sequence = ['u', 'v']

def flatten_helper(sequence, flattened_sequence):
    for item in sequence:
        if isinstance(item, list) or isinstance(item, tuple):
            flatten_helper(item, flattened_sequence)
        flattened_sequence.append(item)

def flatten(sequence):
    """Recursively create a list of all hereditary members of `sequence`."""
    result = [sequence]
    flatten_helper(sequence, result)
    return result

display = {
    'first_sequence': first_sequence,
    'second_sequence': second_sequence,
    'get_bar_value': bar,
    'third_sequence': third_sequence,
    'fourth_sequence': fourth_sequence,
}

class TestCase(EZIOTestCase):

    target_template = 'tuple_unpacking'

    def get_display(self):
        return display

    def get_refcountables(self):
        refcountables = [bar]
        for sequence in display.values():
            refcountables.extend(flatten(sequence))
        return refcountables

    def test(self):
        super(TestCase, self).test()

        expected_lines = [
                'begin first test',
                'a b',
                'c d',
                'end first test',
                'begin second test',
                'x z',
                'b d',
                'end second test',
                'begin third test',
                bar,
                'i j',
                'k l',
                'end third test',
                'begin fourth test',
                'u v',
                'end fourth test',
        ]

        assert_equal(self.lines, expected_lines)

if __name__ == '__main__':
    testify.run()
