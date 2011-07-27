"""
    tmpl2py
    ~~~~~~~

    This module performs a source-to-source transformation of template
    files to Python. The resulting Python is semantically devoid, but the
    `ast` module can parse it, and then other code can use the tools
    provided therein to manipulate a faithful representation of the
    structure of the original template file.

    Its operation is fairly straightforward. The template file is conceptually
    made of line directives (set off by a ``#`` as the first non-whitespace
    character on a line), placeholders (set off by ``$``), and literal text
    (everything else). Directives turn into Python statements, placeholders
    turn into expression statements, and literal text turns into just a string
    literal by itself (which is also an expression statement).

    Terminology:
        * Strategy: a class that defines a .accepts() and .consume() method.
          The .accepts() method is called with the current head of the
          input, and its return value indicates whether or not it wants to
          process it. If it returns ``True`` from the .accepts() method,
          then its .consume() method will be called, and a couple objects
          for manipulating the state of the inputs and outputs are passed
          in. The strategy then performs some manipulation on the inputs
          and outputs, and returns control to the caller.
         * SuperStrategy: Just a strategy that delegates further processing to
           other strategies.

    There are three main strategies, LineDirectiveSuperStrategy,
    PlaceholderSuperStrategy, and LiteralTextStrategy, and they each parse
    chunks respective to their namesakes.

    The main entry points to this module are ``tmpl2py()`` and
    ``tmpl2PyOut()``.
"""

import ast
import re
import tokenize

from collections import namedtuple
from StringIO import StringIO

class EzioLexError(Exception):
    pass

class EzioDedentTooFarError(EzioLexError):
    pass

class EzioInvalidDirectiveError(EzioLexError):
    pass

class EzioNotDollarSignError(EzioLexError):
    pass

class EzioNoStratAcceptedError(EzioLexError):
    pass

class EzioUnmatchedDirectivesError(EzioLexError):
    pass

# ==================================================
#  Functions for helping with Cheetah compatibility
# ==================================================

# These two functions perform similar operations, but ``sanitize_dollars``
# fully removes all the invalid dollar signs from a string in preparation for
# output as valid Python that is representative the input, whereas
# ``idempotent_de_dollar`` returns a modification of the string that is
# syntactically valid iff the result of ``sanitize_dollars`` would be
# syntactically valid, but the character count and relative character positions
# are left unchanged (hence "idempotent").

# TODO: rearrange things so that this is nearer to most of the munging code
MungePair = namedtuple('MungePair', 'munge unmunge')

IDENTITY_MUNGEPAIR = MungePair(*(lambda s: s,) * 2)

def sanitize_dollars(string, munge_pair=IDENTITY_MUNGEPAIR):
    """Remove Cheetah placeholder '$'s from otherwise valid Python.

    The optional parameter ``munge_pair`` specifies an pair of functions that
    put ``string`` in a context that ast.parse() will accept. E.g., to test
    whether an ``elif`` line is valid, it must be put in a context with an
    ``if`` before it.

    Repeatedly call ast.parse() on the string, and strip out dollar signs for
    which it throws a SyntaxError. Raise an exception if the SyntaxError wasn't
    caused by a dollar sign.
    """

    munge, unmunge = munge_pair
    string = munge(string)

    # while ast.parse(string) raises a SyntaxError on a '$', delete that '$'
    while True:

        # XXX: I think I could use verified_SynErr_idx or SynErr_idx here and
        # even be able to get the condition into the while loop
        try:
            ast.parse(string)
        except SyntaxError as e:
            lineno, colno = e.lineno - 1, e.offset - 1 # we want zero-indexed
            lines = string.split('\n')

            if lines[lineno][colno] != '$':
                raise EzioNotDollarSignError(lines[lineno][colno], lineno, colno)

            # delete the offending '$'
            lines[lineno] = lines[lineno][:colno] + lines[lineno][colno+1:]
            string = '\n'.join(lines)
            continue
        else:
            break # all invalid dollar signs are gone, string is valid

    return unmunge(string)

def idempotent_de_dollar(string):
    """Remove Cheetah placeholder '$'s from otherwise valid Python (making
    it syntactically valid) without affecting character count or relative
    character positions.
    """
    return string.replace('$', '_')

# =====================================
#  Strategy for consuming literal text
# =====================================

# a ``#`` is the first non-whitespace character
DIRECTIVE_REGEX = re.compile('^\s*#')
# the Cheetah metacharacters: $, #, and \
# (the \ and the $ are regex metacharacters and must be escaped here)
METACHARACTER_REGEX = re.compile(r'#|\\|\$')

# a group of an odd number of dollar signs
UNESCAPED_DOLLAR = re.compile( r'''
        (?<!{dollar})           # not preceded by a dollar
        ((?:{dollar}{dollar})*) # even number of dollars (i.e. escaped)
                                # captured as group 1
        {dollar}                # a dollar sign
        (?!{dollar})            # not followed by a dollar
        '''.format(dollar=re.escape('$'))
        , re.MULTILINE|re.VERBOSE)

class LiteralTextStrategy(object):
    """Grab a chunk of literal text."""

    def accepts(self, string):
        return True

    # This cannot be called when head is a directive line.
    # i.e., LiteralTextStrategy should be at the end of the strategy list
    # TODO: maybe it would be better to invert the sense of this, so that
    # LiteralTextStrategy consumes anything *except* the things that
    # LineDirectiveSuperStrategy and PlaceholderSuperStrategy should consume.
    def consume(self, py_out, driver):
        consumed = ''

        # loop until we encounter the end of of literal text
        while True:

            # search for any of our special characters
            metacharacter_match = METACHARACTER_REGEX.search(driver.head)

            # read "continue" as "we're still in literal text" and "break" as "we're now outside it"
            if metacharacter_match:
                metacharacter = metacharacter_match.group(0)
                start_pos = metacharacter_match.start(0)
                before_metacharacter = driver.head[:start_pos]
                # TODO if a line begins with whitespace and a #, should we suppress the whitespace?
                consumed += before_metacharacter

                # read the character following the metacharacter
                subsequent_pos = start_pos + 1
                subsequent_char = driver.head[subsequent_pos] if subsequent_pos < len(driver.head) \
                        else None

                # OK, skip over the consumed text; leave the metacharacter for now
                driver.advance_past(before_metacharacter)

                if metacharacter == "\\":
                    if subsequent_char in ("#", "$"):
                        # subsequent char is an escaped metacharacter, consume and skip it
                        # i.e., \# means #, and \$ means $
                        driver.advance_past(driver.head[:2])
                        consumed += subsequent_char
                        continue
                    else:
                        # next char is not a metacharacter; write the backslash
                        # i.e., \a means \a, and \<newline> means \<newline>
                        driver.advance_past(driver.head[:1])
                        consumed += "\\"
                        continue
                elif metacharacter == "#":
                    # unescaped # --- break out into directive mode
                    py_out.commit_line(repr(consumed) + '\n')
                    break
                elif metacharacter == "$":
                    if subsequent_char.isalpha() or subsequent_char in ('_', '(', '[', '{'):
                        # this is the start of a valid Python identifer,
                        # or a Cheetah placeholder block. break out:
                        py_out.commit_line(repr(consumed) + '\n')
                        break
                    else:
                        # this is just a $, e.g., $100.00
                        driver.advance_past(driver.head[:1])
                        consumed += "$"
                        continue
                else:
                    # regex matched a non-metacharacter; this should never happen
                    raise ValueError(metacharacter)

            # Consume the current line.
            # Only one line is ever in driver.head during this loop.
            consumed += driver.head
            driver.advance_past(driver.head)
            #invariant: at this point, driver.head is empty
            assert driver.head == ''

            # get a new line
            if driver.extend_head() is not None: # i.e. we have reached EOF
                # Make sure we consumed something.
                # When a directive is the last line in the file, this will
                # terminate us without adding a spurious newline.
                if consumed != '':
                    py_out.commit_line(repr(consumed) + '\n')
                break

            # else continue (implicitly)


# ==========================================
#  Directive strategies and supporting code
# ==========================================

# Many strategies work by attempting to find a string starting at the current
# head of the input and which parses to valid Python. Due to the way that
# Python's parser works, often some amount of munging is required to put a
# clause header in the correct context for Python's parser to give meaningful
# results. For example, the header of a ``for`` loop needs to have ``\n\tpass``
# appended to it in order to be parsed.

def mk_mungepair(prefix, suffix, compound=False):
    """Return a pair of functions for munging and unmunging the given
    affixes.
    """

    if compound:
        # add in the ':' for cheetah compatibility
        munger = lambda s: prefix + s[:-1] + ':\n' + suffix
        unmunger = lambda s: s[len(prefix) : len(s)-len(suffix)]
    else:
        munger = lambda s: prefix + s + suffix
        unmunger = lambda s: s[len(prefix) : len(s)-len(suffix)]

    return MungePair(munger, unmunger)

# Cut out the key to use for getting the munger/unmunger pair;
# we're looking for either an @ or the alphabetical characters of a Python keyword.
# This pattern has the important property of always matching, even if it is
# the empty string, so we can safely .group(0) off of a .match() call.
MUNGE_KEYGETTER = re.compile('^(@|[A-Za-z]*)')

_munge_pairs = (

    (('if', 'for', 'with', 'while', 'def', 'class'),
        mk_mungepair('', '\tpass', compound=True)),

    (('try',),
        mk_mungepair('', '\tpass\nexcept:\n\tpass', compound=True)),

    (('except', 'finally'),
        mk_mungepair('try:\n\tpass\n', '\tpass', compound=True)),

    (('else', 'elif'),
        mk_mungepair('if True:\n\tpass\n', '\tpass', compound=True)),

    # simple statements
    (('assert', 'pass', 'del', 'return', 'yield', 'raise', 'break',
            'continue', 'import', 'from', 'global', 'exec'),
        IDENTITY_MUNGEPAIR),

    # XXX it is a "simple statement", in that it doesn't need an indented
    # suite. This is a hack, so that custom directives can have decorators.
    (('@',), mk_mungepair('', 'def foo():\n\tpass'))
)

# XXX overly coupled with their position in _munge_pairs
simple_stmts = set(_munge_pairs[-1][0] + _munge_pairs[-2][0])

# build MUNGE_MAP
MUNGE_MAP = {}
for kwds, fn_pair in _munge_pairs:
    for kwd in kwds:
        MUNGE_MAP[kwd] = fn_pair

MUNGEABLE_KEYS = set(MUNGE_MAP.iterkeys())

def get_accepting_strategy(string, strategies):
    """Return the strategy that accepts the given string."""
    for strat in strategies:
        if strat.accepts(string):
            return strat
    else:
        raise EzioNoStratAcceptedError(string)

class LinewisePurePythonStrategy(object):
    """When the directive to be matched is pure-Python and which
    syntactically (in Python) is followed by a NEWLINE token.
    """

    def accepts(self, string):
        return MUNGE_KEYGETTER.match(string).group(0) in MUNGEABLE_KEYS

    def consume(self, py_out, driver):
        """Choose a munge_pair based on the beginning of driver.head, then use
        it to consume linewise until valid Python is encountered.
        """
        kwd = MUNGE_KEYGETTER.match(driver.head).group(0)
        munge_pair = MUNGE_MAP[kwd]

        # dedent for continued compound statements
        if kwd in ('else', 'elif', 'except', 'finally'):
            py_out.dedent()

        for prefix in driver.increasing_prefixes('#\n'):
            munged = munge_pair.munge(prefix)
            try:
                ast.parse(idempotent_de_dollar(munged))
            except SyntaxError:
                continue
            else:
                break

        py_out.commit_line(sanitize_dollars(prefix, munge_pair))
        if kwd not in simple_stmts:
            py_out.indent() # need to indent the suite
        driver.advance_past(prefix)

class EndSuiteStrategy(object):
    """Just dedent."""

    def accepts(self, string):
        return string.startswith('end')

    def consume(self, py_out, driver):
        # read up to a # or a \n, then stop
        for prefix in driver.increasing_prefixes('#\n'):
            driver.advance_past(prefix)
            py_out.dedent()
            return
        # EOF without a newline:
        py_out.dedent()

class ExtendsStrategy(object):
    """Handle conversion of #extends.

    The strategy here is to convert::
        #extends base
    to::
        import base as __extends__
    This will create a distinguished node in the resulting Python AST.
    """

    def accepts(self, string):
        return string.startswith('extends ')

    def consume(self, py_out, driver):
        # remove /^extends / and /\n$/
        name = driver.head[8:-1]
        import_line = 'import %s as __extends__\n' % (name,)

        py_out.commit_line(import_line)
        driver.advance_past(driver.head)

class CallStrategy(object):
    """Handle conversion of #call.

    The strategy here is to convert::
        #call self.layout_container classname=$container_class, end_tag=False
    to::
        with self.layout.container(classname=container_class, end_tag=False) as __call__:
    """

    # Need an appropriate munge/unmunge pair to be able to use
    # sanitize_dollars() to clear dollar signs from the arglist to the #call
    munge_pair = mk_mungepair('foo(', ')')

    def accepts(self, string):
        return string.startswith('call ')

    def consume(self, py_out, driver):
        # remove /^call / and /\n$/
        rest = driver.head[5:-1]

        call_name, _, args = rest.partition(' ')
        sanitized_call_name = sanitize_dollars(call_name)
        sanitized_args = sanitize_dollars(args, self.munge_pair)
        py_out.commit_line('with %s(%s) as __call__:\n' % (sanitized_call_name, sanitized_args))
        py_out.indent()

        driver.advance_past(driver.head)

class SetStrategy(object):
    """Handle conversion of #set, by turning it into an assignment statement."""

    def accepts(self, string):
        return string.startswith('set ')

    def consume(self, py_out, driver):
        # remove /^set/ and /\n$/
        rest = driver.head[4:-1]

        if rest.startswith('global '):
            raise Exception('Set-global unsupported')
        lvalue, _, rvalue = rest.partition('=')
        # generate an ordinary assignment statement
        py_out.commit_line('%s = %s\n' %
                (sanitize_dollars(lvalue.strip()), sanitize_dollars(rvalue.strip())))

        driver.advance_past(driver.head)

class CommentStrategy(object):
    """Substrategy for directives; "directives" beginning with #, i.e.,
    lines beginning with ##, are one-line comments and should be ignored.
    """

    def accepts(self, string):
        return string.startswith('#')

    def consume(self, py_out, driver):
        """Consume the entire line; don't write anything."""
        driver.advance_past(driver.head)


class BlockStrategy(object):
    """Handle conversion of #block.

    The strategy here is to convert::
        #block segment_biz_info_box
    into::
        def DIRECTIVE__block__segment_biz_info_box():
    """

    def accepts(self, string):
        return string.startswith('block ')

    def consume(self, py_out, driver):
        # remove /^block / and /\n$/
        rest = driver.head[6:-1]

        name = rest.strip()

        # TODO: this is more a user error, not an assert. Need a versatile
        # exception that can capture and present this information to the user.
        assert bool(PY_IDENTIFIER.match(name)), "invalid block identifier"

        py_out.commit_line('def DIRECTIVE__block__%s():\n' % (name,))
        py_out.indent()

        driver.advance_past(driver.head)


class LineDirectiveSuperStrategy(object):
    """Super-strategy for dealing with line directives.

    Enters line directive mode, strips (advances past) the '#', calls a
    substrategy, and then exits line directive mode.
    """

    sub_strategies = (CommentStrategy(), BlockStrategy(), CallStrategy(), ExtendsStrategy(),
            SetStrategy(), LinewisePurePythonStrategy(), EndSuiteStrategy())

    def accepts(self, string):
        return bool(DIRECTIVE_REGEX.match(string))

    def consume(self, py_out, driver):
        """Advance past the initial ``#``, enter directive mode, then
        delegate to a substrategy. Finally, exit directive mode.
        """

        driver.in_directive_mode = True

        driver.advance_past(DIRECTIVE_REGEX.match(driver.head).group(0))

        strat = get_accepting_strategy(driver.head, self.sub_strategies)
        strat.consume(py_out, driver)

        driver.in_directive_mode = False


# ============================================
#  Placeholder strategies and supporting code
# ============================================


def SynErr_idx(string):
    """Return the index of a SyntaxError, or None."""

    try:
        ast.parse(string)
    except SyntaxError as e:
        return e.offset - 1 # we want zero-indexed

def verified_SynErr_idx(string, verify_char):
    """Checks that the syntax error was on the right character, and that
    everything up to but not including it is valid.
    """
    index = SynErr_idx(string)
    if index is not None and \
            string[index] == verify_char and \
            SynErr_idx(string[:index]) is None:
        return index

MATCHING_DELIMS = { '(': ')', '[': ']', '{': '}' }

class DollarBracketStrategy(object):
    """Process placeholders of the form $(foo), $[foo], ${foo}."""

    def accepts(self, string):
        return string[0] in MATCHING_DELIMS.iterkeys()

    def consume(self, py_out, driver):
        """Strip beginning delimiter, and then look for a syntax error on its
        matching end delimiter.
        """

        end_delim = MATCHING_DELIMS[driver.head[0]]

        for prefix in driver.increasing_prefixes(end_delim):
            start_bracket_stripped = prefix[1:]
            if verified_SynErr_idx(
                    idempotent_de_dollar(start_bracket_stripped),
                    end_delim):
                break

        py_out.commit_line(sanitize_dollars(prefix[1:-1]) + '\n')
        driver.advance_past(prefix)

PY_IDENTIFIER = re.compile('^[a-zA-Z_][a-zA-Z0-9_]*')
DOTTED_ATTR = re.compile('^\.[a-zA-Z_][a-zA-Z0-9_]*')

class DollarNoBracketStrategy(object):
    """Process placeholders of the form $foo.bar['baz'](quz)."""

    def accepts(self, string):
        return PY_IDENTIFIER.match(string)

    def consume(self, py_out, driver):
        """Consume the initial identifier, then consume one of dotted attribute
        lookup, subscription/slicing, or function invocation for each iteration
        through the loop until unable to consume more.
        """

        consumed = ''

        ident = PY_IDENTIFIER.match(driver.head).group(0)

        consumed += ident
        driver.advance_past(ident)

        dummy_ident = 'foo'

        while True: # terminates when none of the ifs match

            match = DOTTED_ATTR.match(driver.head)
            if match:
                consumed += match.group(0)
                driver.advance_past(match.group(0))
                continue

            if driver.head and driver.head[0] in '[(':
                for prefix in driver.increasing_prefixes(MATCHING_DELIMS[driver.head[0]]):

                    # foo[<inner_slice_or_subscription>] is syntactically valid
                    # where prefix == '[<inner_slice_or_subscription>]'
                    # or
                    # foo(<inner_arglist>) is syntactically valid
                    # where prefix == '(<inner_arglist>)'
                    if SynErr_idx(idempotent_de_dollar(dummy_ident + prefix)) is None:
                        consumed += prefix
                        driver.advance_past(prefix)
                        break
                continue

            break

        py_out.commit_line(sanitize_dollars(consumed) + '\n')

class PlaceholderSuperStrategy(object):

    sub_strategies = (DollarBracketStrategy(), DollarNoBracketStrategy())

    def accepts(self, string):
        # starts with '$', but not '$$'
        if len(string) < 2:
            return False
        else:
            return string[0] == '$' and not string[1] == '$'

    def consume(self, py_out, driver):

        driver.advance_past('$')

        strat = get_accepting_strategy(driver.head, self.sub_strategies)
        strat.consume(py_out, driver)


# =========================
#  Non-strategy components
# =========================


# convenience entry points for the module

def tmpl2py(filelike, strategies=[LineDirectiveSuperStrategy(),
                                  PlaceholderSuperStrategy(),
                                  LiteralTextStrategy()]):
    """Given a template, return the converted Python."""
    py_out = PyOut(filelike, strategies)
    return py_out.mainloop()

def tmpl2PyOut(filelike, strategies=[LineDirectiveSuperStrategy(),
                                     PlaceholderSuperStrategy(),
                                     LiteralTextStrategy()]):
    """Given a template, return an initialized PyOut instance for that
    template.
    """
    py_out = PyOut(filelike, strategies)
    return py_out

def cleanse_whitespace(string):
    """Remove unnecessary whitespace and fit everything on one line."""

    gen = tokenize.generate_tokens(StringIO(string).readline)
    # filter all physical newline (NL) tokens, but not logical (NEWLINE) tokens
    filtered = (tok for tok in gen if tok[0] != tokenize.NL)

    result = []

    for tok_type, tok_str, _, _, _ in filtered:
        # make string literals fit on one line
        if tok_type == tokenize.STRING:
            tok_str = repr(eval(tok_str))

        if tok_str:
            result.append(tok_str)

    return ' '.join(result)

Pos = namedtuple('Pos', 'lineno col_offset')

def calculate_new_pos(string, pos):
    """Compute a new line and column position after adding in the given
    string.
    """

    lineno, col_offset = pos

    if '\n' not in string:
        col_offset += len(string)
    else:
        col_offset = len(string) - string.rindex('\n')
        lineno += string.count('\n')

    return Pos(lineno, col_offset)


class OutBuf(object):
    """Keep track of position in output buffer."""

    def __init__(self):

        self.pos = Pos(lineno=1, col_offset=1)
        self.outbuf = []

    def add_to_buf(self, string):
        self.pos = calculate_new_pos(string, self.pos)

        self.outbuf.append(string)

    def result(self):
        return ''.join(self.outbuf)


class PyOut(object):
    """Coordinate building Python output."""

    def __init__(self, filelike, strategies, indent_with='\t'):

        self.driver = EzioTemplateDriver(filelike)
        self.out_buf = OutBuf()

        self.strategies = strategies
        self.cur_indent = 0
        self.indent_with = indent_with

        # A map between positions in the template file and the generated
        # Python, to be able to give more informative messages farther down the
        # pipeline.
        #
        # keys: .py position
        # values: .tmpl position
        self.pos_map = {}

    def commit_line(self, line):
        """Commit a line to the output buffer.

        Strings passed to this should have a terminating newline.
        """
        cleansed = cleanse_whitespace(line)
        self.out_buf.add_to_buf(self.indent_with * self.cur_indent + cleansed)

        self.pos_map[self.out_buf.pos] = self.driver.pos

    def indent(self):
        self.cur_indent += 1

    def dedent(self):
        if self.cur_indent == 0:
            raise EzioDedentTooFarError(self.driver.pos)
        self.cur_indent -= 1

    def mainloop(self):
        """Drive the conversion process to completion."""
        while not self.driver.done:
            if self.driver.head == '':
                self.driver.extend_head()
                # TODO: could detect EOF here
            strat = get_accepting_strategy(self.driver.head, self.strategies)
            strat.consume(self, self.driver)
        self.final_python = self.out_buf.result()
        if self.cur_indent != 0: # didn't dedent enough!
            raise EzioUnmatchedDirectivesError()
        return self.final_python


class EzioTemplateDriver(object):
    """Present an interface tailored for picking apart template files.

    Keeps track of position in file and provides the all-important
    increasing_prefixes() generator.
    """

    def __init__(self, filelike):

        # convenience conversion from str
        if isinstance(filelike, str):
            filelike = StringIO(filelike)

        self.file = filelike
        # position self.head stars at
        self.pos = Pos(lineno=1, col_offset=1)

        self.done = False
        self.in_directive_mode = False
        self.head = ''

    # the string given to this should always be a slice of ``head``
    def advance_past(self, string):
        """Advance the head of the buffer past the given string and update the
        current position.
        """

        assert self.head.startswith(string), "Advance past what?"

        self.pos = calculate_new_pos(string, self.pos)
        self.head = self.head[len(string):]

    def extend_head(self):
        """Add a new line to head in a way that is aware of being in directive
        mode.
        """

        line = self.file.readline()
        if line == '':
            self.done = True
            return '' # XXX indicates EOF to LiteralTextStrategy

        if self.in_directive_mode:
            if not DIRECTIVE_REGEX.match(line):
                raise EzioInvalidDirectiveError(self.pos)
            line = DIRECTIVE_REGEX.sub('', line, count=1)

        self.head += line

    def increasing_prefixes(self, chars):
        """Return increasing prefixes ending with a char in `chars`.

        This is the secret sauce for plucking out syntactically valid Python
        from the template.
        """

        beginpos = 0
        # make a regex matching any character in chars
        # don't worry, re.compile caches compiled regexes
        regex_str = '|'.join(re.escape(char) for char in chars)
        regex = re.compile(regex_str)

        while beginpos != len(self.head): # have more to process

            for idx in (m.end(0) for m in regex.finditer(self.head, beginpos)):
                yield self.head[:idx]
            beginpos = len(self.head)

            # get more to process
            self.extend_head()


# =======
#  Notes
# =======


#TODO: Cheetah compat:
#
# * minor
#     * "sanitize_dollars": filter dollar signs out of directives and
#         placeholders (pure-Python) (status: DONE)
#     * no ':' at the end of compound statement headers (just add back ':' in
#         munge_prefix) (status: DONE)
# * more major (will require tightly coupled cooperation during AST processing)
#     * cheetah-specific directives (call, attr, etc.)
#         * accomplished by tagging identifiers with __call__ or whatever the
#             custom directive is called. (status: mostly done)

# Ideas for dealing with custom directives:

# for #attr, make::
#    __attr__[attrname] = attrvalue
# for #set, make an assignment statement::
#    lhs = rhs
# for ## (comment), write a strategy that just advances over it without
# committing to the output buffer.

# Currently desupported Cheetah features:
# * inline directives: ``#if len($more_attr_groups)>1#none#else#block#end if#``
# * one-line #call: ``#call self.inline_script: foo("$bar");``
