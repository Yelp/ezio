import testify

from tools.tests.test_case import EZIOTestCase

"""
This is the benchmark from Spitfire, modified slightly.
In the original benchmark, the dict values were ints, not strings,
but right now we just do naive "".join(transaction) at the end
so it chokes on ints. An open TODO is to implement a customized
version of string join that catches type problems and converts
to strings.
"""

table = [dict(a='1',b='2',c='3',d='4',e='5',f='6',g='7',h='8',i='9',j='10')
          for x in range(1000)]

display = {'table': table}

class TestCase(EZIOTestCase):
    target_template = 'bigtable'

    def get_display(self):
        return display

    def get_refcountables(self):
        return [display, display['table'], display.keys()[0]]

if __name__ == '__main__':
    testify.run()
