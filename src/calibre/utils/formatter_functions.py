#!/usr/bin/env python

'''
Created on 13 Jan 2011

@author: charles
'''


__license__   = 'GPL v3'
__copyright__ = '2010, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import inspect
import numbers
import os.path
import posixpath
import re
import traceback
from contextlib import suppress
from datetime import datetime, timedelta
from enum import Enum, auto
from functools import partial
from math import ceil, floor, modf, trunc

from calibre import human_readable, prepare_string_for_xml, prints
from calibre.constants import DEBUG
from calibre.db.constants import DATA_DIR_NAME, DATA_FILE_PATTERN
from calibre.ebooks.metadata import title_sort
from calibre.ebooks.metadata.book.base import field_metadata
from calibre.ebooks.metadata.search_internet import qquote
from calibre.utils.config import tweaks
from calibre.utils.date import UNDEFINED_DATE, format_date, now, parse_date
from calibre.utils.icu import capitalize, sort_key, strcmp
from calibre.utils.icu import lower as icu_lower
from calibre.utils.localization import _ as xlated
from calibre.utils.localization import calibre_langcode_to_name, canonicalize_lang
from calibre.utils.titlecase import titlecase
from polyglot.builtins import iteritems, itervalues

UNKNOWN = _('Unknown')
RELATIONAL = _('Relational')
STRING_MANIPULATION = _('String manipulation')
IF_THEN_ELSE = _('If-then-else')
ARITHMETIC = _('Arithmetic')
RECURSION = _('Recursion')
OTHER = _('Other')
LIST_MANIPULATION = _('List manipulation')
LIST_LOOKUP = _('List lookup')
GET_FROM_METADATA = _('Get values from metadata')
ITERATING_VALUES = _('Iterate over values')
BOOLEAN = _('Boolean')
FORMATTING_VALUES = _('Formatting values')
CASE_CHANGES = _('Case changes')
DATE_FUNCTIONS = _('Date functions')
DB_FUNCS = _('Database functions')
URL_FUNCTIONS = _('URL functions')


# Class and method to save an untranslated copy of translated strings
class TranslatedStringWithRaw(str):

    def __new__(cls, raw_english, raw_other, formatted_english, formatted_other, msgid):
        instance = super().__new__(cls, formatted_other)
        instance.raw_english = raw_english
        instance.raw_other = raw_other
        instance.formatted_english = formatted_english
        instance.formatted_other = formatted_other
        instance.msgid = msgid
        instance.did_format = False
        return instance

    def format(self, *args, **kw):
        formatted_english = self.raw_english.format(*args, **kw)
        formatted_other = self.raw_other.format(*args, **kw)
        v = TranslatedStringWithRaw(self.raw_english, self.raw_other, formatted_english, formatted_other, self.msgid)
        v.saved_args = args
        v.saved_kwargs = kw
        v.did_format = True
        return v

    def format_again(self, txt):
        if self.did_format:
            return txt.format(*self.saved_args, **self.saved_kwargs)
        return txt


def translate_ffml(txt):
    from calibre.utils.ffml_processor import FFMLProcessor
    msgid = FFMLProcessor().document_to_transifex(txt, '', safe=True).strip()
    translated = xlated(msgid)
    if translated == msgid:
        translated = txt
    return TranslatedStringWithRaw(txt, translated, txt, translated, msgid)


class StoredObjectType(Enum):
    PythonFunction = auto()
    StoredGPMTemplate = auto()
    StoredPythonTemplate = auto()


class FormatterFunctions:

    error_function_body = ('def evaluate(self, formatter, kwargs, mi, locals):\n'
                           '\treturn "' +
                            _('Duplicate user function name {0}. '
                              'Change the name or ensure that the functions are identical') + '"')

    def __init__(self):
        self._builtins = {}
        self._functions = {}
        self._functions_from_library = {}

    def register_builtin(self, func_class):
        if not isinstance(func_class, FormatterFunction):
            raise ValueError(f'Class {func_class.__class__.__name__} is not an instance of FormatterFunction')
        name = func_class.name
        if name in self._functions:
            raise ValueError(f'Name {name} already used')
        self._builtins[name] = func_class
        self._functions[name] = func_class
        for a in func_class.aliases:
            self._functions[a] = func_class

    def _register_function(self, func_class, replace=False):
        if not isinstance(func_class, FormatterFunction):
            raise ValueError(f'Class {func_class.__class__.__name__} is not an instance of FormatterFunction')
        name = func_class.name
        if not replace and name in self._functions:
            raise ValueError(f'Name {name} already used')
        self._functions[name] = func_class

    def register_functions(self, library_uuid, funcs):
        self._functions_from_library[library_uuid] = funcs
        self._register_functions()

    def _register_functions(self):
        for compiled_funcs in itervalues(self._functions_from_library):
            for cls in compiled_funcs:
                f = self._functions.get(cls.name, None)
                replace = False
                if f is not None:
                    existing_body = f.program_text
                    new_body = cls.program_text
                    if new_body != existing_body:
                        # Change the body of the template function to one that will
                        # return an error message. Also change the arg count to
                        # -1 (variable) to avoid template compilation errors
                        if DEBUG:
                            print(f'attempt to replace formatter function {f.name} with a different body')
                        replace = True
                        func = [cls.name, '', -1, self.error_function_body.format(cls.name)]
                        cls = compile_user_function(*func)
                    else:
                        continue
                formatter_functions()._register_function(cls, replace=replace)

    def unregister_functions(self, library_uuid):
        if library_uuid in self._functions_from_library:
            for cls in self._functions_from_library[library_uuid]:
                self._functions.pop(cls.name, None)
            self._functions_from_library.pop(library_uuid)
            self._register_functions()

    def get_builtins(self):
        return self._builtins

    def get_builtins_and_aliases(self):
        res = {}
        for f in itervalues(self._builtins):
            res[f.name] = f
            for a in f.aliases:
                res[a] = f
        return res

    def get_functions(self):
        return self._functions

    def reset_to_builtins(self):
        self._functions = {}
        for n,c in self._builtins.items():
            self._functions[n] = c
            for a in c.aliases:
                self._functions[a] = c


_ff = FormatterFunctions()


def formatter_functions():
    global _ff
    return _ff


def only_in_gui_error(name):
    raise ValueError(_('The function {} can be used only in the GUI').format(name))


def get_database(mi, name):
    try:
        proxy = mi.get('_proxy_metadata', None)
    except Exception:
        proxy = None
    if proxy is None:
        if name is not None:
            only_in_gui_error(name)
        return None
    wr = proxy.get('_db', None)
    if wr is None:
        if name is not None:
            raise ValueError(_('In function {}: The database has been closed').format(name))
        return None
    cache = wr()
    if cache is None:
        if name is not None:
            raise ValueError(_('In function {}: The database has been closed').format(name))
        return None
    wr = getattr(cache, 'database_instance', None)
    if wr is None:
        if name is not None:
            only_in_gui_error(name)
        return None
    db = wr()
    if db is None:
        if name is not None:
            raise ValueError(_('In function {}: The database has been closed').format(name))
        return None
    return db


class FormatterFunction:

    name = 'no name provided'
    category = UNKNOWN
    arg_count = 0
    aliases = []
    object_type = StoredObjectType.PythonFunction
    _cached_program_text = None

    def __doc__getter__(self) -> str:
        return _('No documentation provided')

    @property
    def doc(self):
        return self.__doc__getter__()

    @property
    def __doc__(self):
        return self.__doc__getter__()

    @property
    def program_text(self) -> str:
        if self._cached_program_text is None:
            eval_func = inspect.getmembers(self.__class__,
                            lambda x: inspect.isfunction(x) and x.__name__ == 'evaluate')
            try:
                lines = [l[4:] for l in inspect.getsourcelines(eval_func[0][1])[0]]
            except Exception:
                lines = []
            self._cached_program_text = ''.join(lines)
        return self._cached_program_text

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        raise NotImplementedError()

    def eval_(self, formatter, kwargs, mi, locals, *args):
        ret = self.evaluate(formatter, kwargs, mi, locals, *args)
        if isinstance(ret, (bytes, str)):
            return ret
        if isinstance(ret, list):
            return ','.join(ret)
        if isinstance(ret, (numbers.Number, bool)):
            return str(ret)

    def only_in_gui_error(self):
        only_in_gui_error(self.name)

    def get_database(self, mi, formatter=None):
        # Prefer the db that comes from proxy_metadata because it is probably an
        # instance of LibraryDatabase where the one in the formatter might be an
        # instance of Cache
        formatter_db = getattr(formatter, 'database', None)
        if formatter_db is None:
            # The formatter doesn't have a database. Try to get one from
            # proxy_metadata. This will raise an exception because the name
            # parameter is not None
            return get_database(mi, self.name)
        else:
            # We have a formatter db. Try to get the db from proxy_metadata but
            # don't raise an exception if one isn't available.
            legacy_db = get_database(mi, None)
            return legacy_db if legacy_db is not None else formatter_db


class BuiltinFormatterFunction(FormatterFunction):

    def __init__(self):
        formatter_functions().register_builtin(self)


class BuiltinStrcmp(BuiltinFormatterFunction):
    name = 'strcmp'
    arg_count = 5
    category = RELATIONAL
    def __doc__getter__(self): return translate_ffml(
r'''
``strcmp(x, y, lt, eq, gt)`` -- does a case-insensitive lexical comparison of
``x`` and ``y``.[/] Returns ``lt`` if ``x < y``, ``eq`` if ``x == y``, otherwise
``gt``. This function can often be replaced by one of the lexical comparison
operators (``==``, ``>``, ``<``, etc.)
''')

    def evaluate(self, formatter, kwargs, mi, locals, x, y, lt, eq, gt):
        v = strcmp(x, y)
        if v < 0:
            return lt
        if v == 0:
            return eq
        return gt


class BuiltinStrcmpcase(BuiltinFormatterFunction):
    name = 'strcmpcase'
    arg_count = 5
    category = RELATIONAL
    def __doc__getter__(self): return translate_ffml(
r'''
``strcmpcase(x, y, lt, eq, gt)`` -- does a case-sensitive lexical comparison of
``x`` and ``y``.[/] Returns ``lt`` if ``x < y``, ``eq`` if ``x == y``, otherwise
``gt``.

Note: This is NOT the default behavior used by calibre, for example, in the
lexical comparison operators (``==``, ``>``, ``<``, etc.). This function could
cause unexpected results, preferably use ``strcmp()`` whenever possible.
''')

    def evaluate(self, formatter, kwargs, mi, locals, x, y, lt, eq, gt):
        from calibre.utils.icu import case_sensitive_strcmp as case_strcmp
        v = case_strcmp(x, y)
        if v < 0:
            return lt
        if v == 0:
            return eq
        return gt


class BuiltinCmp(BuiltinFormatterFunction):
    name = 'cmp'
    category = RELATIONAL
    arg_count = 5
    def __doc__getter__(self): return translate_ffml(
r'''
``cmp(value, y, lt, eq, gt)`` -- compares ``value`` and ``y`` after converting both to
numbers.[/] Returns ``lt`` if ``value <# y``, ``eq`` if ``value ==# y``, otherwise ``gt``.
This function can usually be replaced with one of the numeric compare operators
(``==#``, ``<#``, ``>#``, etc).
''')

    def evaluate(self, formatter, kwargs, mi, locals, value, y, lt, eq, gt):
        value = float(value if value and value != 'None' else 0)
        y = float(y if y and y != 'None' else 0)
        if value < y:
            return lt
        if value == y:
            return eq
        return gt


class BuiltinFirstMatchingCmp(BuiltinFormatterFunction):
    name = 'first_matching_cmp'
    category = RELATIONAL
    arg_count = -1
    def __doc__getter__(self): return translate_ffml(
r'''
``first_matching_cmp(val, [ cmp, result, ]* else_result)`` -- compares ``val < cmp``
in sequence, returning the associated ``result`` for the first comparison that
succeeds.[/] Returns ``else_result`` if no comparison succeeds.

Example:
[CODE]
i = 10;
first_matching_cmp(i,5,"small",10,"middle",15,"large","giant")
[/CODE]
returns ``"large"``. The same example with a first value of 16 returns ``"giant"``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        if (len(args) % 2) != 0:
            raise ValueError(_('first_matching_cmp requires an even number of arguments'))
        val = float(args[0] if args[0] and args[0] != 'None' else 0)
        for i in range(1, len(args) - 1, 2):
            c = float(args[i] if args[i] and args[i] != 'None' else 0)
            if val < c:
                return args[i+1]
        return args[len(args)-1]


class BuiltinStrcat(BuiltinFormatterFunction):
    name = 'strcat'
    arg_count = -1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``strcat(a [, b]*)`` -- returns a string formed by concatenating all the
arguments.[/] Can take any number of arguments. In most cases you can use the
``&`` operator instead of this function.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        i = 0
        res = ''
        for i in range(len(args)):
            res += args[i]
        return res


class BuiltinStrlen(BuiltinFormatterFunction):
    name = 'strlen'
    arg_count = 1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``strlen(value)`` -- Returns the length of the string ``value``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, a):
        try:
            return len(a)
        except Exception:
            return -1


class BuiltinAdd(BuiltinFormatterFunction):
    name = 'add'
    arg_count = -1
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
'''
``add(x [, y]*)`` -- returns the sum of its arguments.[/] Throws an exception if an
argument is not a number. In most cases you can use the ``+`` operator instead
of this function.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        res = 0
        for v in args:
            v = float(v if v and v != 'None' else 0)
            res += v
        return str(res)


class BuiltinSubtract(BuiltinFormatterFunction):
    name = 'subtract'
    arg_count = 2
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``subtract(x, y)`` -- returns ``x - y``.[/] Throws an exception if either ``x`` or
``y`` are not numbers. This function can usually be replaced by the ``-``
operator.
''')

    def evaluate(self, formatter, kwargs, mi, locals, x, y):
        x = float(x if x and x != 'None' else 0)
        y = float(y if y and y != 'None' else 0)
        return str(x - y)


class BuiltinMultiply(BuiltinFormatterFunction):
    name = 'multiply'
    arg_count = -1
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``multiply(x [, y]*)`` -- returns the product of its arguments.[/] Throws an
exception if any argument is not a number. This function can usually be replaced
by the ``*`` operator.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        res = 1
        for v in args:
            v = float(v if v and v != 'None' else 0)
            res *= v
        return str(res)


class BuiltinDivide(BuiltinFormatterFunction):
    name = 'divide'
    arg_count = 2
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``divide(x, y)`` -- returns ``x / y``.[/] Throws an exception if either ``x`` or
``y`` are not numbers. This function can usually be replaced by the ``/``
operator.
''')

    def evaluate(self, formatter, kwargs, mi, locals, x, y):
        x = float(x if x and x != 'None' else 0)
        y = float(y if y and y != 'None' else 0)
        return str(x / y)


class BuiltinCeiling(BuiltinFormatterFunction):
    name = 'ceiling'
    arg_count = 1
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``ceiling(value)`` -- returns the smallest integer greater than or equal to ``value``.[/]
Throws an exception if ``value`` is not a number.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value):
        value = float(value if value and value != 'None' else 0)
        return str(ceil(value))


class BuiltinFloor(BuiltinFormatterFunction):
    name = 'floor'
    arg_count = 1
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``floor(value)`` -- returns the largest integer less than or equal to ``value``.[/] Throws
an exception if ``value`` is not a number.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value):
        value = float(value if value and value != 'None' else 0)
        return str(floor(value))


class BuiltinRound(BuiltinFormatterFunction):
    name = 'round'
    arg_count = 1
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``round(value)`` -- returns the nearest integer to ``value``.[/] Throws an exception if
``value`` is not a number.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value):
        value = float(value if value and value != 'None' else 0)
        return str(round(value))


class BuiltinMod(BuiltinFormatterFunction):
    name = 'mod'
    arg_count = 2
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``mod(value, y)`` -- returns the ``floor`` of the remainder of ``value / y``.[/] Throws an
exception if either ``value`` or ``y`` is not a number.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value, y):
        value = float(value if value and value != 'None' else 0)
        y = float(y if y and y != 'None' else 0)
        return str(int(value % y))


class BuiltinFractionalPart(BuiltinFormatterFunction):
    name = 'fractional_part'
    arg_count = 1
    category = ARITHMETIC
    def __doc__getter__(self): return translate_ffml(
r'''
``fractional_part(value)`` -- returns the part of the value after the decimal
point.[/] For example, ``fractional_part(3.14)`` returns ``0.14``. Throws an
exception if ``value`` is not a number.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value):
        value = float(value if value and value != 'None' else 0)
        return str(modf(value)[0])


class BuiltinTemplate(BuiltinFormatterFunction):
    name = 'template'
    arg_count = 1
    category = RECURSION

    def __doc__getter__(self): return translate_ffml(
r'''
``template(x)`` -- evaluates ``x`` as a template.[/] The evaluation is done in its
own context, meaning that variables are not shared between the caller and the
template evaluation.  If not using General Program Mode, because the ``{`` and
``}`` characters are special, you must use ``[[`` for the ``{`` character and
``]]`` for the } character; they are converted automatically. For example,
``template(\'[[title_sort]]\')`` will evaluate the template ``{title_sort}`` and return
its value. Note also that prefixes and suffixes (the ``|prefix|suffix`` syntax)
cannot be used in the argument to this function when using template program
mode.
''')

    def evaluate(self, formatter, kwargs, mi, locals, template):
        template = template.replace('[[', '{').replace(']]', '}')
        return formatter.__class__().safe_format(template, kwargs, 'TEMPLATE', mi)


class BuiltinEval(BuiltinFormatterFunction):
    name = 'eval'
    arg_count = 1
    category = RECURSION
    def __doc__getter__(self): return translate_ffml(
r'''
``eval(string)`` -- evaluates the string as a program, passing the local
variables.[/] This permits using the template processor to construct complex
results from local variables. In
[URL href="https://manual.calibre-ebook.com/template_lang.html#more-complex-programs-in-template-expressions-template-program-mode"]
Template Program Mode[/URL],
because the ``{`` and ``}`` characters are interpreted before the template is
evaluated you must use ``[[`` for the ``{`` character and ``]]`` for the ``}``
character. They are converted automatically. Note also that prefixes and
suffixes (the ``|prefix|suffix`` syntax) cannot be used in the argument to this
function when using Template Program Mode.
''')

    def evaluate(self, formatter, kwargs, mi, locals, template):
        from calibre.utils.formatter import EvalFormatter
        template = template.replace('[[', '{').replace(']]', '}')
        return EvalFormatter().safe_format(template, locals, 'EVAL', None)


class BuiltinAssign(BuiltinFormatterFunction):
    name = 'assign'
    arg_count = 2
    category = OTHER
    def __doc__getter__(self): return translate_ffml(
r'''
``assign(id, value)`` -- assigns ``value`` to ``id``[/], then returns ``value``. ``id``
must be an identifier, not an expression. In most cases you can use the ``=``
operator instead of this function.
''')

    def evaluate(self, formatter, kwargs, mi, locals, target, value):
        locals[target] = value
        return value


class BuiltinListSplit(BuiltinFormatterFunction):
    name = 'list_split'
    arg_count = 3
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_split(list_val, sep, id_prefix)`` -- splits ``list_val`` into separate
values using ``sep``[/], then assigns the values to local variables named
``id_prefix_N`` where N is the position of the value in the list. The first item
has position 0 (zero). The function returns the last element in the list.

Example:
[CODE]
    list_split('one:two:foo', ':', 'var')
[/CODE]
is equivalent to:
[CODE]
    var_0 = 'one'
    var_1 = 'two'
    var_2 = 'foo'
[/CODE]
''')

    def evaluate(self, formatter, kwargs, mi, locals, list_val, sep, id_prefix):
        l = [v.strip() for v in list_val.split(sep)]
        res = ''
        for i,v in enumerate(l):
            res = locals[id_prefix+'_'+str(i)] = v
        return res


class BuiltinPrint(BuiltinFormatterFunction):
    name = 'print'
    arg_count = -1
    category = OTHER
    def __doc__getter__(self): return translate_ffml(
r'''
``print(a [, b]*)`` -- prints the arguments to standard output.[/] Unless you start
calibre from the command line (``calibre-debug -g``), the output will go into a
black hole. The ``print`` function always returns its first argument.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        print(args)
        return ''


class BuiltinField(BuiltinFormatterFunction):
    name = 'field'
    arg_count = 1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``field(lookup_name)`` -- returns the value of the metadata field with lookup name ``lookup_name``.[/]
The ``$`` prefix can be used instead of the function, as in ``$tags``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, name):
        return formatter.get_value(name, [], kwargs)


class BuiltinRawField(BuiltinFormatterFunction):
    name = 'raw_field'
    arg_count = -1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``raw_field(lookup_name [, optional_default])`` -- returns the metadata field
named by ``lookup_name`` without applying any formatting.[/] It evaluates and
returns the optional second argument ``optional_default`` if the field's value
is undefined (``None``). The ``$$`` prefix can be used instead of the function,
as in ``$$pubdate``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, name, default=None):
        res = getattr(mi, name, None)
        if res is None and default is not None:
            return default
        if isinstance(res, list):
            fm = mi.metadata_for_field(name)
            if fm is None:
                return ', '.join(res)
            return fm['is_multiple']['list_to_ui'].join(res)
        return str(res)


class BuiltinRawList(BuiltinFormatterFunction):
    name = 'raw_list'
    arg_count = 2
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``raw_list(lookup_name, separator)`` -- returns the metadata list named by
``lookup_name`` without applying any formatting or sorting[/], with the items
separated by ``separator``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, name, separator):
        res = getattr(mi, name, None)
        if not isinstance(res, list):
            return f'{name} is not a list'
        return separator.join(res)


class BuiltinSubstr(BuiltinFormatterFunction):
    name = 'substr'
    arg_count = 3
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``substr(value, start, end)`` -- returns the ``start``'th through the ``end``'th
characters of ``value``.[/] The first character in ``value`` is the zero'th character.
If ``end`` is negative then it indicates that many characters counting from the
right. If ``end`` is zero, then it indicates the last character. For example,
``substr('12345', 1, 0)`` returns ``'2345'``, and ``substr('12345', 1, -1)``
returns ``'234'``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value, start_, end_):
        return value[int(start_): len(value) if int(end_) == 0 else int(end_)]


class BuiltinLookup(BuiltinFormatterFunction):
    name = 'lookup'
    arg_count = -1
    category = ITERATING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``lookup(value, [ pattern, key, ]* else_key)`` -- The patterns will be checked against
the ``value`` in order.[/] If a ``pattern`` matches then the value of the field named by
``key`` is returned. If no pattern matches then the value of the field named by
``else_key`` is returned. See also the :ref:`switch` function.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, *args):
        if len(args) == 2:  # here for backwards compatibility
            if val:
                return formatter.vformat('{'+args[0].strip()+'}', [], kwargs)
            else:
                return formatter.vformat('{'+args[1].strip()+'}', [], kwargs)
        if (len(args) % 2) != 1:
            raise ValueError(_('lookup requires either 2 or an odd number of arguments'))
        i = 0
        while i < len(args):
            if i + 1 >= len(args):
                return formatter.vformat('{' + args[i].strip() + '}', [], kwargs)
            if re.search(args[i], val, flags=re.I):
                return formatter.vformat('{'+args[i+1].strip() + '}', [], kwargs)
            i += 2


class BuiltinTest(BuiltinFormatterFunction):
    name = 'test'
    arg_count = 3
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``test(value, text_if_not_empty, text_if_empty)`` -- return ``text_if_not_empty`` if
the value is not empty, otherwise return ``text_if_empty``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, value_if_set, value_not_set):
        if val:
            return value_if_set
        else:
            return value_not_set


class BuiltinContains(BuiltinFormatterFunction):
    name = 'contains'
    arg_count = 4
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``contains(value, pattern, text_if_match, text_if_not_match)`` -- checks if the value
is matched by the regular expression ``pattern``.[/] Returns ``text_if_match`` if
the pattern matches the value, otherwise returns ``text_if_not_match``.
''')

    def evaluate(self, formatter, kwargs, mi, locals,
                 val, test, value_if_present, value_if_not):
        if re.search(test, val, flags=re.I):
            return value_if_present
        else:
            return value_if_not


class BuiltinSwitch(BuiltinFormatterFunction):
    name = 'switch'
    arg_count = -1
    category = ITERATING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``switch(value, [patternN, valueN,]+ else_value)`` -- for each ``patternN, valueN`` pair,
checks if the ``value`` matches the regular expression ``patternN``[/] and if so returns
the associated ``valueN``. If no ``patternN`` matches, then ``else_value`` is
returned. You can have as many ``patternN, valueN`` pairs as you wish. The first
match is returned.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, *args):
        if (len(args) % 2) != 1:
            raise ValueError(_('switch requires an even number of arguments'))
        i = 0
        while i < len(args):
            if i + 1 >= len(args):
                return args[i]
            if re.search(args[i], val, flags=re.I):
                return args[i+1]
            i += 2


class BuiltinSwitchIf(BuiltinFormatterFunction):
    name = 'switch_if'
    arg_count = -1
    category = ITERATING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``switch_if([test_expression, value_expression,]+ else_expression)`` -- for each
``test_expression, value_expression`` pair, checks if ``test_expression`` is
True (non-empty) and if so returns the result of ``value_expression``.[/] If no
``test_expression`` is True then the result of ``else_expression`` is returned.
You can have as many ``test_expression, value_expression`` pairs as you want.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        if (len(args) % 2) != 1:
            raise ValueError(_('switch_if requires an odd number of arguments'))
        # We shouldn't get here because the function is inlined. However, someone
        # might call it directly.
        i = 0
        while i < len(args):
            if i + 1 >= len(args):
                return args[i]
            if args[i]:
                return args[i+1]
            i += 2


class BuiltinStrcatMax(BuiltinFormatterFunction):
    name = 'strcat_max'
    arg_count = -1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``strcat_max(max, string1 [, prefix2, string2]*)`` -- Returns a string formed by
concatenating the arguments.[/] The returned value is initialized to ``string1``.
Strings made from ``prefix, string`` pairs are added to the end of the value as
long as the resulting string length is less than ``max``. Prefixes can be empty.
Returns ``string1`` even if ``string1`` is longer than ``max``. You can pass as
many ``prefix, string`` pairs as you wish.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        if len(args) < 2:
            raise ValueError(_('strcat_max requires 2 or more arguments'))
        if (len(args) % 2) != 0:
            raise ValueError(_('strcat_max requires an even number of arguments'))
        try:
            max = int(args[0])
        except Exception:
            raise ValueError(_('first argument to strcat_max must be an integer'))

        i = 2
        result = args[1]
        try:
            while i < len(args):
                if (len(result) + len(args[i]) + len(args[i+1])) > max:
                    break
                result = result + args[i] + args[i+1]
                i += 2
        except Exception:
            pass
        return result.strip()


class BuiltinInList(BuiltinFormatterFunction):
    name = 'list_contains'
    arg_count = -1
    category = LIST_LOOKUP
    def __doc__getter__(self): return translate_ffml(
r'''
``list_contains(value, separator, [ pattern, found_val, ]* not_found_val)`` -- interpret the
``value`` as a list of items separated by ``separator``, checking the ``pattern``
against each item in the list.[/] If the ``pattern`` matches an item then return
``found_val``, otherwise return ``not_found_val``. The pair ``pattern`` and
``found_value`` can be repeated as many times as desired, permitting returning
different values depending on the item's value. The patterns are checked in
order, and the first match is returned.

Aliases: in_list(), list_contains()
''')

    aliases = ['in_list']

    def evaluate(self, formatter, kwargs, mi, locals, val, sep, *args):
        if (len(args) % 2) != 1:
            raise ValueError(_('in_list requires an odd number of arguments'))
        l = [v.strip() for v in val.split(sep) if v.strip()]
        i = 0
        while i < len(args):
            if i + 1 >= len(args):
                return args[i]
            sf = args[i]
            fv = args[i+1]
            if l:
                for v in l:
                    if re.search(sf, v, flags=re.I):
                        return fv
            i += 2


class BuiltinStrInList(BuiltinFormatterFunction):
    name = 'str_in_list'
    arg_count = -1
    category = LIST_LOOKUP
    def __doc__getter__(self): return translate_ffml(
r'''
``str_in_list(value, separator, [ string, found_val, ]+ not_found_val)`` -- interpret
the ``value`` as a list of items separated by ``separator`` then compare ``string``
against each value in the list.[/] The ``string`` is not a regular expression. If
``string`` is equal to any item (ignoring case) then return the corresponding
``found_val``. If ``string`` contains ``separators`` then it is also treated as
a list and each subvalue is checked. The ``string`` and ``found_value`` pairs
can be repeated as many times as desired, permitting returning different values
depending on string's value. If none of the strings match then
``not_found_value`` is returned. The strings are checked in order. The first
match is returned.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, sep, *args):
        if (len(args) % 2) != 1:
            raise ValueError(_('str_in_list requires an odd number of arguments'))
        l = [v.strip() for v in val.split(sep) if v.strip()]
        i = 0
        while i < len(args):
            if i + 1 >= len(args):
                return args[i]
            sf = args[i]
            fv = args[i+1]
            c = [v.strip() for v in sf.split(sep) if v.strip()]
            if l:
                for v in l:
                    for t in c:
                        if strcmp(t, v) == 0:
                            return fv
            i += 2


class BuiltinIdentifierInList(BuiltinFormatterFunction):
    name = 'identifier_in_list'
    arg_count = -1
    category = LIST_LOOKUP
    def __doc__getter__(self): return translate_ffml(
r'''
``identifier_in_list(val, id_name [, found_val, not_found_val])`` -- treat
``val`` as a list of identifiers separated by commas. An identifier has the
format ``id_name:value``.[/] The ``id_name`` parameter is the id_name text to
search for, either ``id_name`` or ``id_name:regexp``. The first case matches if
there is any identifier matching that id_name. The second case matches if
id_name matches an identifier and the regexp matches the identifier's value. If
``found_val`` and ``not_found_val`` are provided then if there is a match then
return ``found_val``, otherwise return ``not_found_val``. If ``found_val`` and
``not_found_val`` are not provided then if there is a match then return the
``identifier:value`` pair, otherwise the empty string (``''``).
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, ident, *args):
        if len(args) == 0:
            fv_is_id = True
            nfv = ''
        elif len(args) == 2:
            fv_is_id = False
            fv = args[0]
            nfv = args[1]
        else:
            raise ValueError(_('{} requires 2 or 4 arguments').format(self.name))

        l = [v.strip() for v in val.split(',') if v.strip()]
        id_, __, regexp = ident.partition(':')
        if not id_:
            return nfv
        for candidate in l:
            i, __, v = candidate.partition(':')
            if v and i == id_:
                if not regexp or re.search(regexp, v, flags=re.I):
                    return candidate if fv_is_id else fv
        return nfv


class BuiltinRe(BuiltinFormatterFunction):
    name = 're'
    arg_count = 3
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``re(value, pattern, replacement)`` -- return the ``value`` after applying the regular
expression.[/] All instances of ``pattern`` in the value are replaced with
``replacement``. The template language uses case insensitive
[URL href="https://docs.python.org/3/library/re.html"]Python regular
expressions[/URL].
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, pattern, replacement):
        return re.sub(pattern, replacement, val, flags=re.I)


class BuiltinReGroup(BuiltinFormatterFunction):
    name = 're_group'
    arg_count = -1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``re_group(value, pattern [, template_for_group]*)`` --  return a string made by
applying the regular expression ``pattern`` to ``value`` and replacing each matched
instance[/] with the value returned by the corresponding template. In
[URL href="https://manual.calibre-ebook.com/template_lang.html#more-complex-programs-in-template-expressions-template-program-mode"]
Template Program Mode[/URL], like for the ``template`` and the
``eval`` functions, you use ``[[`` for ``{`` and ``]]`` for ``}``.

The following example looks for a series with more than one word and uppercases the first word:
[CODE]
program: re_group(field('series'), "(\S* )(.*)", "{$:uppercase()}", "{$}")'}
[/CODE]
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, pattern, *args):
        from calibre.utils.formatter import EvalFormatter

        def repl(mo):
            res = ''
            if mo and mo.lastindex:
                for dex in range(mo.lastindex):
                    gv = mo.group(dex+1)
                    if gv is None:
                        continue
                    if len(args) > dex:
                        template = args[dex].replace('[[', '{').replace(']]', '}')
                        res += EvalFormatter().safe_format(template, {'$': gv},
                                           'EVAL', None, strip_results=False)
                    else:
                        res += gv
            return res
        return re.sub(pattern, repl, val, flags=re.I)


class BuiltinSwapAroundComma(BuiltinFormatterFunction):
    name = 'swap_around_comma'
    arg_count = 1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``swap_around_comma(value)`` -- given a ``value`` of the form ``B, A``, return ``A B``.[/]
This is most useful for converting names in LN, FN format to FN LN. If there is
no comma in the ``value`` then the function returns the value unchanged.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        return re.sub(r'^(.*?),\s*(.*$)', r'\2 \1', val, flags=re.I).strip()


class BuiltinIfempty(BuiltinFormatterFunction):
    name = 'ifempty'
    arg_count = 2
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``ifempty(value, text_if_empty)`` -- if the ``value`` is not empty then return that ``value``,
otherwise return ``text_if_empty``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, value_if_empty):
        if val:
            return val
        else:
            return value_if_empty


class BuiltinShorten(BuiltinFormatterFunction):
    name = 'shorten'
    arg_count = 4
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``shorten(value, left_chars, middle_text, right_chars)`` -- Return a shortened version
of the ``value``[/], consisting of ``left_chars`` characters from the beginning of the
``value``, followed by ``middle_text``, followed by ``right_chars`` characters from
the end of the ``value``. ``left_chars`` and ``right_chars`` must be non-negative
integers.

Example: assume you want to display the title with a length of at most
15 characters in length. One template that does this is
``{title:shorten(9,-,5)}``. For a book with the title `Ancient English Laws in
the Times of Ivanhoe` the result will be `Ancient E-anhoe`: the first 9
characters of the title, a ``-``, then the last 5 characters. If the value's
length is less than ``left chars`` + ``right chars`` + the length of ``middle text``
then the value will be returned unchanged. For example, the title `The
Dome` would not be changed.
''')

    def evaluate(self, formatter, kwargs, mi, locals,
                 val, leading, center_string, trailing):
        l = max(0, int(leading))
        t = max(0, int(trailing))
        if len(val) > l + len(center_string) + t:
            return val[0:l] + center_string + ('' if t == 0 else val[-t:])
        else:
            return val


class BuiltinCount(BuiltinFormatterFunction):
    name = 'list_count'
    arg_count = 2
    category = LIST_MANIPULATION
    aliases = ['count']

    def __doc__getter__(self): return translate_ffml(
r'''
``list_count(value, separator)`` -- interprets the value as a list of items separated by
``separator`` and returns the number of items in the list.[/] Most lists use
a comma as the separator, but ``authors`` uses an ampersand (&).

Examples: ``{tags:list_count(,)}``, ``{authors:list_count(&)}``.

Aliases: ``count()``, ``list_count()``
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, sep):
        return str(len([v for v in val.split(sep) if v]))


class BuiltinListCountMatching(BuiltinFormatterFunction):
    name = 'list_count_matching'
    arg_count = 3
    category = LIST_MANIPULATION
    aliases = ['count_matching']

    def __doc__getter__(self): return translate_ffml(
r'''
``list_count_matching(value, pattern, separator)`` -- interprets ``value`` as a
list of items separated by ``separator``, returning the number of items in the
list that match the regular expression ``pattern``.[/]

Aliases: ``list_count_matching()``, ``count_matching()``
''')

    def evaluate(self, formatter, kwargs, mi, locals, value, pattern, sep):
        res = 0
        for v in [x.strip() for x in value.split(sep) if x.strip()]:
            if re.search(pattern, v, flags=re.I):
                res += 1
        return str(res)


class BuiltinListitem(BuiltinFormatterFunction):
    name = 'list_item'
    arg_count = 3
    category = LIST_LOOKUP
    def __doc__getter__(self): return translate_ffml(
r'''
``list_item(value, index, separator)`` -- interpret the ``value`` as a list of items
separated by ``separator``, returning the 'index'th item.[/] The first item is
number zero. The last item has the index ``-1`` as in
``list_item(-1,separator)``. If the item is not in the list, then the empty
string is returned. The separator has the same meaning as in the count function,
usually comma but is ampersand for author-like lists.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, index, sep):
        if not val:
            return ''
        index = int(index)
        val = val.split(sep)
        try:
            return val[index].strip()
        except Exception:
            return ''


class BuiltinSelect(BuiltinFormatterFunction):
    name = 'select'
    arg_count = 2
    category = LIST_LOOKUP
    def __doc__getter__(self): return translate_ffml(
r'''
``select(value, key)`` -- interpret the ``value`` as a comma-separated list of items with
each item having the form ``id:id_value`` (the calibre ``identifier`` format).[/] The
function finds the first pair with the id equal to ``key`` and returns the
corresponding ``id_value``. If no id matches then the function returns the empty
string.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, key):
        if not val:
            return ''
        vals = [v.strip() for v in val.split(',')]
        tkey = key+':'
        for v in vals:
            if v.startswith(tkey):
                return v[len(tkey):]
        return ''


class BuiltinApproximateFormats(BuiltinFormatterFunction):
    name = 'approximate_formats'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``approximate_formats()`` -- return a comma-separated list of formats associated
with the book.[/] Because the list comes from calibre's database instead of the
file system, there is no guarantee that the list is correct, although it
probably is. Note that resulting format names are always uppercase, as in EPUB.
The ``approximate_formats()`` function is much faster than the ``formats_...``
functions.

This function works only in the GUI. If you want to use these values in save-to-disk
or send-to-device templates then you must make a custom "Column built from
other columns", use the function in that column's template, and use that
column's value in your save/send templates.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        if hasattr(mi, '_proxy_metadata'):
            fmt_data = mi._proxy_metadata.db_approx_formats
            if not fmt_data:
                return ''
            data = sorted(fmt_data)
            return ','.join(v.upper() for v in data)
        self.only_in_gui_error()


class BuiltinFormatsModtimes(BuiltinFormatterFunction):
    name = 'formats_modtimes'
    arg_count = 1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``formats_modtimes(date_format_string)`` -- return a comma-separated list of
colon-separated items ``FMT:DATE`` representing modification times for the
formats of a book.[/] The ``date_format_string`` parameter specifies how the date
is to be formatted. See the :ref:`format_date` function for details. You can use
the :ref:`select` function to get the modification time for a specific format. Note
that format names are always uppercase, as in EPUB.
''')

    def evaluate(self, formatter, kwargs, mi, locals, fmt):
        fmt_data = mi.get('format_metadata', {})
        try:
            data = sorted(fmt_data.items(), key=lambda x:x[1]['mtime'], reverse=True)
            return ','.join(k.upper()+':'+format_date(v['mtime'], fmt)
                        for k,v in data)
        except Exception:
            return ''


class BuiltinFormatsSizes(BuiltinFormatterFunction):
    name = 'formats_sizes'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''

``formats_sizes()`` -- return a comma-separated list of colon-separated
``FMT:SIZE`` items giving the sizes of the formats of a book in bytes.[/] You can
use the ``select()`` function to get the size for a specific format. Note that
format names are always uppercase, as in EPUB.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        fmt_data = mi.get('format_metadata', {})
        try:
            return ','.join(k.upper()+':'+str(v['size']) for k,v in iteritems(fmt_data))
        except Exception:
            return ''


class BuiltinFormatsPaths(BuiltinFormatterFunction):
    name = 'formats_paths'
    arg_count = -1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``formats_paths([separator])`` -- return a ``separator``-separated list of
colon-separated items ``FMT:PATH`` giving the full path to the formats of a
book.[/] The ``separator`` argument is optional. If not supplied then the
separator is ``', '`` (comma space). If the separator is a comma then you can
use the ``select()`` function to get the path for a specific format. Note that
format names are always uppercase, as in EPUB.
''')

    def evaluate(self, formatter, kwargs, mi, locals, sep=','):
        fmt_data = mi.get('format_metadata', {})
        try:
            return sep.join(k.upper()+':'+str(v['path']) for k,v in iteritems(fmt_data))
        except Exception:
            return ''


class BuiltinFormatsPathSegments(BuiltinFormatterFunction):
    name = 'formats_path_segments'
    arg_count = 5
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``formats_path_segments(with_author, with_title, with_format, with_ext, sep)``
-- return parts of the path to a book format in the calibre library separated
by ``sep``.[/] The parameter ``sep`` should usually be a slash (``'/'``). One use
is to be sure that paths generated in Save to disk and Send to device templates
are shortened consistently. Another is to be sure the paths on the device match
the paths in the calibre library.

A book path consists of 3 segments: the author, the title including the calibre
database id in parentheses, and the format (author - title). Calibre can
shorten any of the three because of file name length limitations. You choose
which segments to include by passing ``1`` for that segment. If you don't want
a segment then pass ``0`` or the empty string for that segment. For example,
the following returns just the format name without the extension:
[CODE]
formats_path_segments(0, 0, 1, 0, '/')
[/CODE]
Because there is only one segment the separator is ignored.

If there are multiple formats (multiple extensions) then one of the extensions
will be picked at random. If you care about which extension is used then get
the path without the extension then add the desired extension to it.

Examples: Assume there is a book in the calibre library with an epub format by
Joe Blogs with title 'Help'. It would have the path
[CODE]
Joe Blogs/Help - (calibre_id)/Help - Joe Blogs.epub
[/CODE]
The following shows what is returned for various parameters:
[LIST]
[*]``formats_path_segments(0, 0, 1, 0, '/')`` returns `Help - Joe Blogs`
[*]``formats_path_segments(0, 0, 1, 1, '/')`` returns `Help - Joe Blogs.epub`
[*]``formats_path_segments(1, 0, 1, 1, '/')`` returns `Joe Blogs/Help - Joe Blogs.epub`
[*]``formats_path_segments(1, 0, 1, 0, '/')`` returns `Joe Blogs/Help - Joe Blogs`
[*]``formats_path_segments(0, 1, 0, 0, '/')`` returns `Help - (calibre_id)`
[/LIST]
''')

    def evaluate(self, formatter, kwargs, mi, locals, with_author, with_title, with_format, with_ext, sep):
        fmt_metadata = mi.get('format_metadata', {})
        if fmt_metadata:
            for v in fmt_metadata.values():
                p = v['path']
                r,fmt = os.path.split(p)
                if with_ext == '0' or not with_ext:
                    fmt = os.path.splitext(fmt)[0]
                r,title = os.path.split(r)
                r,author  = os.path.split(r)
                parts = []
                if with_author == '1':
                    parts.append(author)
                if with_title == '1':
                    parts.append(title)
                if with_format == '1':
                    parts.append(fmt)
                return sep.join(parts)
        else:
            return _("No book formats found so the path can't be generated")


class BuiltinHumanReadable(BuiltinFormatterFunction):
    name = 'human_readable'
    arg_count = 1
    category = FORMATTING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``human_readable(value)`` -- expects the ``value`` to be a number and returns a string
representing that number in KB, MB, GB, etc.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        try:
            return human_readable(round(float(val)))
        except Exception:
            return ''


class BuiltinFormatNumber(BuiltinFormatterFunction):
    name = 'format_number'
    arg_count = 2
    category = FORMATTING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``format_number(value, template)`` -- interprets the ``value`` as a number and formats that
number using a Python formatting template such as ``{0:5.2f}`` or ``{0:,d}`` or
``${0:5,.2f}``.[/] The formatting template must begin with ``{0:`` and end with
``}`` as in the above examples. Exception: you can leave off the leading "{0:"
and trailing "}" if the format template contains only a format. See the
[URL href="https://manual.calibre-ebook.com/template_lang.html"]
Template Language[/URL] and the
[URL href="https://docs.python.org/3/library/string.html#formatstrings"]
Python[/URL] documentation for more examples. Returns the empty string if formatting fails.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, template):
        if val == '' or val == 'None':
            return ''
        if '{' not in template:
            template = '{0:' + template + '}'
        try:
            v1 = float(val)
        except Exception:
            return ''
        try:  # Try formatting the value as a float
            return template.format(v1)
        except Exception:
            pass
        try:  # Try formatting the value as an int
            v2 = trunc(v1)
            if v2 == v1:
                return template.format(v2)
        except Exception:
            pass
        return ''


class BuiltinSublist(BuiltinFormatterFunction):
    name = 'sublist'
    arg_count = 4
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``sublist(value, start_index, end_index, separator)`` -- interpret the ``value`` as a
list of items separated by ``separator``, returning a new list made from the
items from ``start_index`` to ``end_index``.[/] The first item is number zero. If
an index is negative, then it counts from the end of the list. As a special
case, an end_index of zero is assumed to be the length of the list.

Examples assuming that the tags column (which is comma-separated) contains "A, B, C":
[LIST]
[*]``{tags:sublist(0,1,\,)}`` returns "A"
[*]``{tags:sublist(-1,0,\,)}`` returns "C"
[*]``{tags:sublist(0,-1,\,)}`` returns "A, B"
[/LIST]
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, start_index, end_index, sep):
        if not val:
            return ''
        si = int(start_index)
        ei = int(end_index)
        # allow empty list items so counts are what the user expects
        val = [v.strip() for v in val.split(sep)]

        if sep == ',':
            sep = ', '
        try:
            if ei == 0:
                return sep.join(val[si:])
            else:
                return sep.join(val[si:ei])
        except Exception:
            return ''


class BuiltinSubitems(BuiltinFormatterFunction):
    name = 'subitems'
    arg_count = 3
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``subitems(value, start_index, end_index)`` -- This function breaks apart lists of
tag-like hierarchical items such as genres.[/] It interprets the ``value`` as a comma-
separated list of tag-like items, where each item is a period-separated list. It
returns a new list made by extracting from each item the components from
``start_index`` to ``end_index``, then merging the results back together.
Duplicates are removed. The first subitem in a period-separated list has an
index of zero. If an index is negative then it counts from the end of the list.
As a special case, an ``end_index`` of zero is assumed to be the length of the list.

Examples:
[LIST]
[*]Assuming a #genre column containing "A.B.C":
[LIST]
[*]``{#genre:subitems(0,1)}`` returns "A"
[*]``{#genre:subitems(0,2)}`` returns "A.B"
[*]``{#genre:subitems(1,0)}`` returns "B.C"
[/LIST]
[*]Assuming a #genre column containing "A.B.C, D.E":
[LIST]
[*]``{#genre:subitems(0,1)}`` returns "A, D"
[*]``{#genre:subitems(0,2)}`` returns "A.B, D.E"
[/LIST]
[/LIST]
''')

    period_pattern = re.compile(r'(?<=[^\.\s])\.(?=[^\.\s])', re.U)

    def evaluate(self, formatter, kwargs, mi, locals, val, start_index, end_index):
        if not val:
            return ''
        si = int(start_index)
        ei = int(end_index)
        has_periods = '.' in val
        items = [v.strip() for v in val.split(',') if v.strip()]
        rv = set()
        for item in items:
            if has_periods and '.' in item:
                components = self.period_pattern.split(item)
            else:
                components = [item]
            try:
                if ei == 0:
                    t = '.'.join(components[si:]).strip()
                else:
                    t = '.'.join(components[si:ei]).strip()
                if t:
                    rv.add(t)
            except Exception:
                pass
        return ', '.join(sorted(rv, key=sort_key))


class BuiltinFormatDate(BuiltinFormatterFunction):
    name = 'format_date'
    arg_count = 2
    category = FORMATTING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``format_date(value, format_string)`` -- format the ``value``, which must be a date
string, using the ``format_string``, returning a string.[/] It is best if the date is
in ISO format as using other date formats often causes errors because the actual
date value cannot be unambiguously determined. Note that the
``format_date_field()`` function is both faster and more reliable.

The formatting codes are:
[LIST]
[*]``d    :`` the day as number without a leading zero (1 to 31)
[*]``dd   :`` the day as number with a leading zero (01 to 31)
[*]``ddd  :`` the abbreviated localized day name (e.g. "Mon" to "Sun")
[*]``dddd :`` the long localized day name (e.g. "Monday" to "Sunday")
[*]``M    :`` the month as number without a leading zero (1 to 12)
[*]``MM   :`` the month as number with a leading zero (01 to 12)
[*]``MMM  :`` the abbreviated localized month name (e.g. "Jan" to "Dec")
[*]``MMMM :`` the long localized month name (e.g. "January" to "December")
[*]``yy   :`` the year as two digit number (00 to 99)
[*]``yyyy :`` the year as four digit number.
[*]``h    :`` the hours without a leading 0 (0 to 11 or 0 to 23, depending on am/pm)
[*]``hh   :`` the hours with a leading 0 (00 to 11 or 00 to 23, depending on am/pm)
[*]``m    :`` the minutes without a leading 0 (0 to 59)
[*]``mm   :`` the minutes with a leading 0 (00 to 59)
[*]``s    :`` the seconds without a leading 0 (0 to 59)
[*]``ss   :`` the seconds with a leading 0 (00 to 59)
[*]``ap   :`` use a 12-hour clock instead of a 24-hour clock, with 'ap' replaced by the lowercase localized string for am or pm
[*]``AP   :`` use a 12-hour clock instead of a 24-hour clock, with 'AP' replaced by the uppercase localized string for AM or PM
[*]``aP   :`` use a 12-hour clock instead of a 24-hour clock, with 'aP' replaced by the localized string for AM or PM
[*]``Ap   :`` use a 12-hour clock instead of a 24-hour clock, with 'Ap' replaced by the localized string for AM or PM
[*]``iso  :`` the date with time and timezone. Must be the only format present
[*]``to_number   :`` convert the date & time into a floating point number (a `timestamp`)
[*]``from_number :`` convert a floating point number (a `timestamp`) into an
ISO-formatted date. If you want a different date format then add the
desired formatting string after ``from_number`` and a colon (``:``). Example:
[CODE]
format_date(val, 'from_number:MMM dd yyyy')
[/CODE]
[/LIST]
You might get unexpected results if the date you are formatting contains
localized month names, which can happen if you changed the date format to
contain ``MMMM``. Using ``format_date_field()`` avoids this problem.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, format_string):
        if not val or val == 'None':
            return ''
        try:
            if format_string == 'to_number':
                s = parse_date(val).timestamp()
            elif format_string.startswith('from_number'):
                val = datetime.fromtimestamp(float(val))
                f = format_string[12:]
                s = format_date(val, f if f else 'iso')
            else:
                s = format_date(parse_date(val), format_string)
            return s
        except Exception:
            s = 'BAD DATE'
        return s


class BuiltinFormatDateField(BuiltinFormatterFunction):
    name = 'format_date_field'
    arg_count = 2
    category = FORMATTING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
 ``format_date_field(field_name, format_string)`` -- format the value in the
 field ``field_name``, which must be the lookup name of a date field, either
 standard or custom.[/] See :ref:`format_date` for the formatting codes. This
 function is much faster than format_date() and should be used when you are
 formatting the value in a field (column). It is also more reliable because it
 works directly on the underlying date. It can't be used for computed dates or
 dates in string variables. Examples:
[CODE]
format_date_field('pubdate', 'yyyy.MM.dd')
format_date_field('#date_read', 'MMM dd, yyyy')
[/CODE]
''')

    def evaluate(self, formatter, kwargs, mi, locals, field, format_string):
        try:
            field = field_metadata.search_term_to_field_key(field)
            if field not in mi.all_field_keys():
                raise ValueError(_("Function {0}: Unknown field '{1}'").format('format_date_field', field))
            val = mi.get(field, None)
            if mi.metadata_for_field(field)['datatype'] != 'datetime':
                raise ValueError(_("Function {0}: field '{1}' is not a date").format('format_date_field', field))
            if val is None:
                s = ''
            elif format_string == 'to_number':
                s = val.timestamp()
            elif format_string.startswith('from_number'):
                val = datetime.fromtimestamp(float(val))
                f = format_string[12:]
                s = format_date(val, f if f else 'iso')
            else:
                s = format_date(val, format_string)
            return s
        except ValueError:
            raise
        except Exception:
            traceback.print_exc()
            raise
        return s


class BuiltinUppercase(BuiltinFormatterFunction):
    name = 'uppercase'
    arg_count = 1
    category = CASE_CHANGES
    def __doc__getter__(self): return translate_ffml(
r'''
``uppercase(value)`` -- returns the ``value`` in upper case.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        return val.upper()


class BuiltinLowercase(BuiltinFormatterFunction):
    name = 'lowercase'
    arg_count = 1
    category = CASE_CHANGES
    def __doc__getter__(self): return translate_ffml(
r'''
``lowercase(value)`` -- returns the ``value`` in lower case.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        return val.lower()


class BuiltinTitlecase(BuiltinFormatterFunction):
    name = 'titlecase'
    arg_count = 1
    category = CASE_CHANGES
    def __doc__getter__(self): return translate_ffml(
r'''
``titlecase(value)`` -- returns the ``value`` in title case.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        return titlecase(val)


class BuiltinCapitalize(BuiltinFormatterFunction):
    name = 'capitalize'
    arg_count = 1
    category = CASE_CHANGES
    def __doc__getter__(self): return translate_ffml(
r'''
``capitalize(value)`` -- returns the ``value`` with the first letter in upper case and the rest lower case.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        return capitalize(val)


class BuiltinBooksize(BuiltinFormatterFunction):
    name = 'booksize'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``booksize()`` -- returns the value of the calibre ``size`` field. Returns '' if the book has no formats.[/]

This function works only in the GUI. If you want to use this value in save-to-disk
or send-to-device templates then you must make a custom "Column built from
other columns", use the function in that column's template, and use that
column's value in your save/send templates
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        if hasattr(mi, '_proxy_metadata'):
            try:
                v = mi._proxy_metadata.book_size
                if v is not None:
                    return str(mi._proxy_metadata.book_size)
                return ''
            except Exception:
                pass
            return ''
        self.only_in_gui_error()


class BuiltinOndevice(BuiltinFormatterFunction):
    name = 'ondevice'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``ondevice()`` -- return the string ``'Yes'`` if ``ondevice`` is set, otherwise
return the empty string.[/] This function works only in the GUI. If you want to use
this value in save-to-disk or send-to-device templates then you must make a
custom "Column built from other columns", use the function in that column\'s
template, and use that column\'s value in your save/send templates.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        if hasattr(mi, '_proxy_metadata'):
            if mi._proxy_metadata.ondevice_col:
                return _('Yes')
            return ''
        self.only_in_gui_error()


class BuiltinAnnotationCount(BuiltinFormatterFunction):
    name = 'annotation_count'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``annotation_count()`` -- return the total number of annotations of all types
attached to the current book.[/] This function works only in the GUI and the
content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        c = self.get_database(mi, formatter=formatter).new_api.annotation_count_for_book(mi.id)
        return '' if c == 0 else str(c)


class BuiltinIsMarked(BuiltinFormatterFunction):
    name = 'is_marked'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``is_marked()`` -- check whether the book is `marked` in calibre.[/] If it is then
return the value of the mark, either ``'true'`` (lower case) or a comma-separated
list of named marks. Returns ``''`` (the empty string) if the book is
not marked. This function works only in the GUI.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        c = self.get_database(mi, formatter=formatter).data.get_marked(mi.id)
        return c if c else ''


class BuiltinSeriesSort(BuiltinFormatterFunction):
    name = 'series_sort'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``series_sort()`` -- returns the series sort value.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        if mi.series:
            langs = mi.languages
            lang = langs[0] if langs else None
            return title_sort(mi.series, lang=lang)
        return ''


class BuiltinHasCover(BuiltinFormatterFunction):
    name = 'has_cover'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``has_cover()`` -- return ``'Yes'`` if the book has a cover, otherwise the empty string.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        if mi.has_cover:
            return _('Yes')
        return ''


class BuiltinFirstNonEmpty(BuiltinFormatterFunction):
    name = 'first_non_empty'
    arg_count = -1
    category = ITERATING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``first_non_empty(value [, value]*)`` -- returns the first ``value`` that is not
empty.[/] If all values are empty, then the empty string is returned. You can have
as many values as you want.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        i = 0
        while i < len(args):
            if args[i]:
                return args[i]
            i += 1
        return ''


class BuiltinAnd(BuiltinFormatterFunction):
    name = 'and'
    arg_count = -1
    category = BOOLEAN
    def __doc__getter__(self): return translate_ffml(
r'''
``and(value [, value]*)`` -- returns the string ``'1'`` if all values are not empty,
otherwise returns the empty string.[/] You can have as many values as you want. In
most cases you can use the ``&&`` operator instead of this function.  One reason
not to replace ``and()`` with ``&&`` is when short-circuiting can change the results
because of side effects. For example, ``and(a='',b=5)`` will always do both
assignments, where the ``&&`` operator won't do the second.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        i = 0
        while i < len(args):
            if not args[i]:
                return ''
            i += 1
        return '1'


class BuiltinOr(BuiltinFormatterFunction):
    name = 'or'
    arg_count = -1
    category = BOOLEAN
    def __doc__getter__(self): return translate_ffml(
r'''
``or(value [, value]*)`` -- returns the string ``'1'`` if any value is not
empty, otherwise returns the empty string.[/] You can have as many values as you
want. This function can usually be replaced by the ``||`` operator. A reason it
cannot be replaced is if short-circuiting will change the results because of
side effects.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        i = 0
        while i < len(args):
            if args[i]:
                return '1'
            i += 1
        return ''


class BuiltinNot(BuiltinFormatterFunction):
    name = 'not'
    arg_count = 1
    category = BOOLEAN
    def __doc__getter__(self): return translate_ffml(
r'''
``not(value)`` -- returns the string ``'1'`` if the value is empty, otherwise
returns the empty string.[/] This function can usually be replaced with the unary
not (``!``) operator.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        return '' if val else '1'


class BuiltinListJoin(BuiltinFormatterFunction):
    name = 'list_join'
    arg_count = -1
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_join(with_separator, list1, separator1 [, list2, separator2]*)`` --
return a list made by joining the items in the source lists[/] (``list1`` etc)
using ``with_separator`` between the items in the result list. Items in each
source ``list[123...]`` are separated by the associated ``separator[123...]``. A
list can contain zero values. It can be a field like ``publisher`` that is
single-valued, effectively a one-item list. Duplicates are removed using a
case-insensitive comparison. Items are returned in the order they appear in the
source lists. If items on lists differ only in letter case then the last is
used. All separators can be more than one character.

Example:
[CODE]
program:
    list_join('#@#', $authors, '&', $tags, ',')
[/CODE]
You can use ``list_join`` on the results of previous calls to ``list_join`` as follows:
[CODE]
program:
    a = list_join('#@#', $authors, '&', $tags, ',');
    b = list_join('#@#', a, '#@#', $#genre, ',', $#people, '&', 'some value', ',')
[/CODE]
You can use expressions to generate a list. For example, assume you want items
for ``authors`` and ``#genre``, but with the genre changed to the word "Genre: "
followed by the first letter of the genre, i.e. the genre "Fiction" becomes
"Genre: F". The following will do that:
{}''').format('''\
[CODE]
program:
    list_join('#@#', $authors, '&', list_re($#genre, ',', '^(.).*$', 'Genre: \\1'),  ',')
[/CODE]
''')  # not translated as \1 gets mistranslated as a control char in transifex
    # for some reason. And yes, the double backslash is required, for some reason.

    def evaluate(self, formatter, kwargs, mi, locals, with_separator, *args):
        if len(args) % 2 != 0:
            raise ValueError(
                _("Invalid 'List, separator' pairs. Every list must have one "
                  "associated separator"))

        # Starting in python 3.7 dicts preserve order so we don't need OrderedDict
        result = {}
        i = 0
        while i < len(args):
            lst = [v.strip() for v in args[i].split(args[i+1]) if v.strip()]
            result.update({item.lower():item for item in lst})
            i += 2
        return with_separator.join(result.values())


class BuiltinListUnion(BuiltinFormatterFunction):
    name = 'list_union'
    arg_count = 3
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_union(list1, list2, separator)`` -- return a list made by merging the
items in ``list1`` and ``list2``[/], removing duplicate items using a case-insensitive
comparison. If items differ in case, the one in ``list1`` is used.
The items in ``list1`` and ``list2`` are separated by ``separator``, as are the
items in the returned list.

Aliases: ``merge_lists()``, ``list_union()``
''')
    aliases = ['merge_lists']

    def evaluate(self, formatter, kwargs, mi, locals, list1, list2, separator):
        res = {icu_lower(l.strip()): l.strip() for l in list2.split(separator) if l.strip()}
        res.update({icu_lower(l.strip()): l.strip() for l in list1.split(separator) if l.strip()})
        if separator == ',':
            separator = ', '
        return separator.join(res.values())


class BuiltinRange(BuiltinFormatterFunction):
    name = 'range'
    arg_count = -1
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``range(start, stop, step, limit)`` -- returns a list of numbers generated by
looping over the range specified by the parameters ``start``, ``stop``, and ``step``,
with a maximum length of ``limit``.[/] The first value produced is 'start'. Subsequent values
``next_v = current_v + step``. The loop continues while ``next_v < stop``
assuming ``step`` is positive, otherwise while ``next_v > stop``. An empty list
is produced if ``start`` fails the test: ``start >= stop`` if ``step`` is
positive. The ``limit`` sets the maximum length of the list and has a default of
1000. The parameters ``start``, ``step``, and ``limit`` are optional. Calling
``range()`` with one argument specifies ``stop``. Two arguments specify
``start`` and ``stop``. Three arguments specify ``start``, ``stop``, and
``step``. Four arguments specify ``start``, ``stop``, ``step`` and ``limit``.

Examples:
[CODE]
range(5) -> '0, 1, 2, 3, 4'
range(0, 5) -> '0, 1, 2, 3, 4'
range(-1, 5) -> '-1, 0, 1, 2, 3, 4'
range(1, 5) -> '1, 2, 3, 4'
range(1, 5, 2) -> '1, 3'
range(1, 5, 2, 5) -> '1, 3'
range(1, 5, 2, 1) -> error(limit exceeded)
[/CODE]
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        limit_val = 1000
        start_val = 0
        step_val = 1
        if len(args) == 1:
            stop_val = int(args[0] if args[0] and args[0] != 'None' else 0)
        elif len(args) == 2:
            start_val = int(args[0] if args[0] and args[0] != 'None' else 0)
            stop_val = int(args[1] if args[1] and args[1] != 'None' else 0)
        elif len(args) >= 3:
            start_val = int(args[0] if args[0] and args[0] != 'None' else 0)
            stop_val = int(args[1] if args[1] and args[1] != 'None' else 0)
            step_val = int(args[2] if args[2] and args[2] != 'None' else 0)
            if len(args) > 3:
                limit_val = int(args[3] if args[3] and args[3] != 'None' else 0)
        r = range(start_val, stop_val, step_val)
        if len(r) > limit_val:
            raise ValueError(
                _('{0}: length ({1}) longer than limit ({2})').format(
                            'range', len(r), str(limit_val)))
        return ', '.join([str(v) for v in r])


class BuiltinListRemoveDuplicates(BuiltinFormatterFunction):
    name = 'list_remove_duplicates'
    arg_count = 2
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_remove_duplicates(list, separator)`` -- return a list made by removing
duplicate items in ``list``.[/] If items differ only in case then the last is
returned. The items in ``list`` are separated by ``separator``, as are the items
in the returned list.
''')

    def evaluate(self, formatter, kwargs, mi, locals, list_, separator):
        res = {icu_lower(l.strip()): l.strip() for l in list_.split(separator) if l.strip()}
        if separator == ',':
            separator = ', '
        return separator.join(res.values())


class BuiltinListDifference(BuiltinFormatterFunction):
    name = 'list_difference'
    arg_count = 3
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_difference(list1, list2, separator)`` -- return a list made by removing
from ``list1`` any item found in ``list2``[/] using a case-insensitive comparison.
The items in ``list1`` and ``list2`` are separated by ``separator``, as are the
items in the returned list.
''')

    def evaluate(self, formatter, kwargs, mi, locals, list1, list2, separator):
        l1 = [l.strip() for l in list1.split(separator) if l.strip()]
        l2 = {icu_lower(l.strip()) for l in list2.split(separator) if l.strip()}

        res = []
        for i in l1:
            if icu_lower(i) not in l2 and i not in res:
                res.append(i)
        if separator == ',':
            return ', '.join(res)
        return separator.join(res)


class BuiltinListIntersection(BuiltinFormatterFunction):
    name = 'list_intersection'
    arg_count = 3
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_intersection(list1, list2, separator)`` -- return a list made by removing
from ``list1`` any item not found in ``list2``[/] using a case-insensitive
comparison. The items in ``list1`` and ``list2`` are separated by ``separator``, as
are the items in the returned list.
''')

    def evaluate(self, formatter, kwargs, mi, locals, list1, list2, separator):
        l1 = [l.strip() for l in list1.split(separator) if l.strip()]
        l2 = {icu_lower(l.strip()) for l in list2.split(separator) if l.strip()}

        res = []
        for i in l1:
            if icu_lower(i) in l2 and i not in res:
                res.append(i)
        if separator == ',':
            return ', '.join(res)
        return separator.join(res)


class BuiltinListSort(BuiltinFormatterFunction):
    name = 'list_sort'
    arg_count = 3
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_sort(value, direction, separator)`` -- return ``value`` sorted using a
case-insensitive lexical sort.[/] If ``direction`` is zero (number or character),
``value`` is sorted ascending, otherwise descending. The list items are separated
by ``separator``, as are the items in the returned list.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value, direction, separator):
        res = [l.strip() for l in value.split(separator) if l.strip()]
        if separator == ',':
            return ', '.join(sorted(res, key=sort_key, reverse=direction != '0'))
        return separator.join(sorted(res, key=sort_key, reverse=direction != '0'))


class BuiltinListEquals(BuiltinFormatterFunction):
    name = 'list_equals'
    arg_count = 6
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_equals(list1, sep1, list2, sep2, yes_val, no_val)`` -- return ``yes_val``
if ``list1`` and ``list2`` contain the same items, otherwise return ``no_val``.[/]
The items are determined by splitting each list using the appropriate separator
character (``sep1`` or ``sep2``). The order of items in the lists is not
relevant. The comparison is case-insensitive.
''')

    def evaluate(self, formatter, kwargs, mi, locals, list1, sep1, list2, sep2, yes_val, no_val):
        s1 = {icu_lower(l.strip()) for l in list1.split(sep1) if l.strip()}
        s2 = {icu_lower(l.strip()) for l in list2.split(sep2) if l.strip()}
        if s1 == s2:
            return yes_val
        return no_val


class BuiltinListRe(BuiltinFormatterFunction):
    name = 'list_re'
    arg_count = 4
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_re(src_list, separator, include_re, opt_replace)`` -- Construct a list by
first separating ``src_list`` into items using the ``separator`` character.[/] For
each item in the list, check if it matches ``include_re``. If it does then add
it to the list to be returned. If ``opt_replace`` is not the empty string then
apply the replacement before adding the item to the returned list.
''')

    def evaluate(self, formatter, kwargs, mi, locals, src_list, separator, include_re, opt_replace):
        l = [l.strip() for l in src_list.split(separator) if l.strip()]
        res = []
        for item in l:
            if re.search(include_re, item, flags=re.I) is not None:
                if opt_replace:
                    item = re.sub(include_re, opt_replace, item)
                for i in [t.strip() for t in item.split(separator) if t.strip()]:
                    if i not in res:
                        res.append(i)
        if separator == ',':
            return ', '.join(res)
        return separator.join(res)


class BuiltinListReGroup(BuiltinFormatterFunction):
    name = 'list_re_group'
    arg_count = -1
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_re_group(src_list, separator, include_re, search_re [,template_for_group]*)``
-- Like ``list_re()`` except replacements are not optional.[/] It
uses ``re_group(item, search_re, template ...)`` when doing the replacements.
''')

    def evaluate(self, formatter, kwargs, mi, locals, src_list, separator, include_re,
                 search_re, *args):
        from calibre.utils.formatter import EvalFormatter

        l = [l.strip() for l in src_list.split(separator) if l.strip()]
        res = []
        for item in l:
            def repl(mo):
                newval = ''
                if mo and mo.lastindex:
                    for dex in range(mo.lastindex):
                        gv = mo.group(dex+1)
                        if gv is None:
                            continue
                        if len(args) > dex:
                            template = args[dex].replace('[[', '{').replace(']]', '}')
                            newval += EvalFormatter().safe_format(template, {'$': gv},
                                              'EVAL', None, strip_results=False)
                        else:
                            newval += gv
                return newval
            if re.search(include_re, item, flags=re.I) is not None:
                item = re.sub(search_re, repl, item, flags=re.I)
                for i in [t.strip() for t in item.split(separator) if t.strip()]:
                    if i not in res:
                        res.append(i)
        if separator == ',':
            return ', '.join(res)
        return separator.join(res)


class BuiltinToday(BuiltinFormatterFunction):
    name = 'today'
    arg_count = 0
    category = DATE_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``today()`` -- return a date+time string for today (now).[/] This value is designed
for use in ``format_date`` or ``days_between``, but can be manipulated like any
other string. The date is in [URL href="https://en.wikipedia.org/wiki/ISO_8601"]ISO[/URL]
date/time format.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        return format_date(now(), 'iso')


class BuiltinDaysBetween(BuiltinFormatterFunction):
    name = 'days_between'
    arg_count = 2
    category = DATE_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``days_between(date1, date2)`` -- return the number of days between ``date1``
and ``date2``.[/] The number is positive if ``date1`` is greater than ``date2``,
otherwise negative. If either ``date1`` or ``date2`` are not dates, the function
returns the empty string.
''')

    def evaluate(self, formatter, kwargs, mi, locals, date1, date2):
        try:
            d1 = parse_date(date1)
            if d1 == UNDEFINED_DATE:
                return ''
            d2 = parse_date(date2)
            if d2 == UNDEFINED_DATE:
                return ''
        except Exception:
            return ''
        i = d1 - d2
        return f'{i.days+(i.seconds/(24.0*60.0*60.0)):.1f}'


class BuiltinDateArithmetic(BuiltinFormatterFunction):
    name = 'date_arithmetic'
    arg_count = -1
    category = DATE_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``date_arithmetic(value, calc_spec, fmt)`` -- Calculate a new date from ``value``
using ``calc_spec``.[/] Return the new date formatted according to optional
``fmt``: if not supplied then the result will be in ISO format. The ``calc_spec`` is
a string formed by concatenating pairs of ``vW`` (``valueWhat``) where ``v`` is
a possibly-negative number and W is one of the following letters:
[LIST]
[*]``s``: add ``v`` seconds to ``date``
[*]``m``: add ``v`` minutes to ``date``
[*]``h``: add ``v`` hours to ``date``
[*]``d``: add ``v`` days to ``date``
[*]``w``: add ``v`` weeks to ``date``
[*]``y``: add ``v`` years to ``date``, where a year is 365 days.
[/LIST]
Example: ``'1s3d-1m'`` will add 1 second, add 3 days, and subtract 1 minute from ``date``.
  ''')

    calc_ops = {
        's': lambda v: timedelta(seconds=v),
        'm': lambda v: timedelta(minutes=v),
        'h': lambda v: timedelta(hours=v),
        'd': lambda v: timedelta(days=v),
        'w': lambda v: timedelta(weeks=v),
        'y': lambda v: timedelta(days=v * 365),
    }

    def evaluate(self, formatter, kwargs, mi, locals, value, calc_spec, fmt=None):
        try:
            d = parse_date(value)
            if d == UNDEFINED_DATE:
                return ''
            while calc_spec:
                mo = re.match(r'([-+\d]+)([smhdwy])', calc_spec)
                if mo is None:
                    raise ValueError(
                        _("{0}: invalid calculation specifier '{1}'").format(
                            'date_arithmetic', calc_spec))
                d += self.calc_ops[mo[2]](int(mo[1]))
                calc_spec = calc_spec[len(mo[0]):]
            return format_date(d, fmt if fmt else 'iso')
        except ValueError as e:
            raise e
        except Exception as e:
            traceback.print_exc()
            raise ValueError(_('{0}: error: {1}').format('date_arithmetic', str(e)))


class BuiltinLanguageStrings(BuiltinFormatterFunction):
    name = 'language_strings'
    arg_count = 2
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``language_strings(value, localize)`` -- return the
language names for the language codes
([URL href="https://www.loc.gov/standards/iso639-2/php/code_list.php"]
see here for names and codes[/URL])
passed in ``value``.[/] Example: ``{languages:language_strings()}``.
If ``localize`` is zero, return the strings in English. If ``localize`` is not zero,
return the strings in the language of the current locale. ``lang_codes`` is a comma-separated list.
''')

    def evaluate(self, formatter, kwargs, mi, locals, lang_codes, localize):
        retval = []
        for c in [c.strip() for c in lang_codes.split(',') if c.strip()]:
            try:
                n = calibre_langcode_to_name(c, localize != '0')
                if n:
                    retval.append(n)
            except Exception:
                pass
        return ', '.join(retval)


class BuiltinLanguageCodes(BuiltinFormatterFunction):
    name = 'language_codes'
    arg_count = 1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``language_codes(lang_strings)`` -- return the
[URL href="https://www.loc.gov/standards/iso639-2/php/code_list.php"]language codes[/URL] for the language
names passed in ``lang_strings``.[/] The strings must be in the language of the
current locale. ``lang_strings`` is a comma-separated list.
''')

    def evaluate(self, formatter, kwargs, mi, locals, lang_strings):
        retval = []
        for c in [c.strip() for c in lang_strings.split(',') if c.strip()]:
            try:
                cv = canonicalize_lang(c)
                if cv:
                    retval.append(canonicalize_lang(cv))
            except Exception:
                pass
        return ', '.join(retval)


class BuiltinCurrentLibraryName(BuiltinFormatterFunction):
    name = 'current_library_name'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``current_library_name()`` -- return the last name on the path to the current calibre library.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        from calibre.library import current_library_name
        return current_library_name()


class BuiltinCurrentLibraryPath(BuiltinFormatterFunction):
    name = 'current_library_path'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``current_library_path()`` -- return the full path to the current calibre
library.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        from calibre.library import current_library_path
        return current_library_path()


class BuiltinFinishFormatting(BuiltinFormatterFunction):
    name = 'finish_formatting'
    arg_count = 4
    category = FORMATTING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``finish_formatting(value, format, prefix, suffix)`` -- apply the ``format``, ``prefix``, and
``suffix`` to the ``value`` in the same way as done in a template like
``{series_index:05.2f| - |- }``.[/] This function is provided to ease conversion of
complex single-function- or template-program-mode templates to `GPM` Templates.
For example, the following program produces the same output as the above
template:
[CODE]
program: finish_formatting(field("series_index"), "05.2f", " - ", " - ")
[/CODE]
Another example: for the template:
[CODE]
{series:re(([^\s])[^\s]+(\s|$),\1)}{series_index:0>2s| - | - }{title}
[/CODE]
use:
[CODE]
program:
    strcat(
        re(field('series'), '([^\s])[^\s]+(\s|$)', '\1'),
        finish_formatting(field('series_index'), '0>2s', ' - ', ' - '),
        field('title')
    )
[/CODE]
''')

    def evaluate(self, formatter, kwargs, mi, locals_, val, fmt, prefix, suffix):
        if not val:
            return val
        return prefix + formatter._do_format(val, fmt) + suffix


class BuiltinVirtualLibraries(BuiltinFormatterFunction):
    name = 'virtual_libraries'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``virtual_libraries()`` -- return a comma-separated list of Virtual libraries that
contain this book.[/] This function works only in the GUI. If you want to use these
values in save-to-disk or send-to-device templates then you must make a custom
`Column built from other columns`, use the function in that column's template,
and use that column's value in your save/send templates.
''')

    def evaluate(self, formatter, kwargs, mi, locals_):
        db = self.get_database(mi, formatter=formatter)
        try:
            a = db.data.get_virtual_libraries_for_books((mi.id,))
            return ', '.join(a[mi.id])
        except ValueError as v:
            return str(v)


class BuiltinCurrentVirtualLibraryName(BuiltinFormatterFunction):
    name = 'current_virtual_library_name'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``current_virtual_library_name()`` -- return the name of the current
virtual library if there is one, otherwise the empty string.[/] Library name case
is preserved. Example:
[CODE]
program: current_virtual_library_name()
[/CODE]
This function works only in the GUI.
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        return self.get_database(mi, formatter=formatter).data.get_base_restriction_name()


class BuiltinUserCategories(BuiltinFormatterFunction):
    name = 'user_categories'
    arg_count = 0
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``user_categories()`` -- return a comma-separated list of the user categories that
contain this book.[/] This function works only in the GUI. If you want to use these
values in save-to-disk or send-to-device templates then you must make a custom
`Column built from other columns`, use the function in that column's template,
and use that column's value in your save/send templates
''')

    def evaluate(self, formatter, kwargs, mi, locals_):
        if hasattr(mi, '_proxy_metadata'):
            cats = {k for k, v in iteritems(mi._proxy_metadata.user_categories) if v}
            cats = sorted(cats, key=sort_key)
            return ', '.join(cats)
        self.only_in_gui_error()


class BuiltinTransliterate(BuiltinFormatterFunction):
    name = 'transliterate'
    arg_count = 1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``transliterate(value)`` -- Return a string in a latin alphabet formed by
approximating the sound of the words in ``value``.[/] For example, if ``value``
is ``{0}`` this function returns ``{1}``.
''').format('Фёдор Миха́йлович Достоевский', 'Fiodor Mikhailovich Dostoievskii')

    def evaluate(self, formatter, kwargs, mi, locals, source):
        from calibre.utils.filenames import ascii_text
        return ascii_text(source)


class BuiltinGetLink(BuiltinFormatterFunction):
    name = 'get_link'
    arg_count = 2
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``get_link(field_name, field_value)`` -- fetch the link for field ``field_name``
with value ``field_value``.[/] If there is no attached link, return the empty
string. Examples:
[LIST]
[*]The following returns the link attached to the tag ``Fiction``:
[CODE]
get_link('tags', 'Fiction')
[/CODE]
[*]This template makes a list of the links for all the tags associated with a
book in the form ``value:link, ...``:
[CODE]
program:
    ans = '';
    for t in $tags:
        l = get_link('tags', t);
        if l then
            ans = list_join(', ', ans, ',', t & ':' & get_link('tags', t), ',')
        fi
    rof;
ans
[/CODE]
[/LIST]
This function works only in the GUI and the content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, field_name, field_value):
        db = self.get_database(mi, formatter=formatter).new_api
        try:
            link = None
            item_id = db.get_item_id(field_name, field_value, case_sensitive=True)
            if item_id is not None:
                link = db.link_for(field_name, item_id)
            return link if link is not None else ''
        except Exception as e:
            traceback.print_exc()
            raise ValueError(e)


class BuiltinAuthorLinks(BuiltinFormatterFunction):
    name = 'author_links'
    arg_count = 2
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``author_links(val_separator, pair_separator)`` -- returns a string containing a
list of authors and those authors' link values[/] in the form:
``author1 val_separator author1_link pair_separator author2 val_separator author2_link`` etc.

An author is separated from its link value by the ``val_separator`` string
with no added spaces. Assuming the ``val_separator`` is a colon,
``author:link value`` pairs are separated by the
``pair_separator`` string argument with no added spaces. It is up to you to
choose separators that do not occur in author names or links. An author
is included even if the author link is empty.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val_sep, pair_sep):
        if hasattr(mi, '_proxy_metadata'):
            link_data = mi._proxy_metadata.link_maps
            if not link_data:
                return ''
            link_data = link_data.get('authors')
            if not link_data:
                return ''
            names = sorted(link_data.keys(), key=sort_key)
            return pair_sep.join(n + val_sep + link_data[n] for n in names)
        self.only_in_gui_error()


class BuiltinAuthorSorts(BuiltinFormatterFunction):
    name = 'author_sorts'
    arg_count = 1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``author_sorts(val_separator)`` -- returns a string containing a list of
author's sort values for the authors of the book.[/] The sort is the one in the
author metadata information, which can be different from the author_sort in books. The
returned list has the form ``author sort 1`` ``val_separator`` ``author sort 2``
etc. with no added spaces. The author sort values in this list are in the same
order as the authors of the book. If you want spaces around ``val_separator``
then include them in the ``val_separator`` string.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val_sep):
        sort_data = mi.author_sort_map
        if not sort_data:
            return ''
        names = [sort_data.get(n) for n in mi.authors if n.strip()]
        return val_sep.join(n for n in names)


class BuiltinConnectedDeviceName(BuiltinFormatterFunction):
    name = 'connected_device_name'
    arg_count = 1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``connected_device_name(storage_location_key)`` -- if a device is connected then
return the device name, otherwise return the empty string.[/] Each storage location
on a device has its own device name. The ``storage_location_key`` names are
``'main'``, ``'carda'`` and ``'cardb'``. This function works only in the GUI.
''')

    def evaluate(self, formatter, kwargs, mi, locals, storage_location):
        # We can't use get_database() here because we need the device manager.
        # In other words, the function really does need the GUI
        with suppress(Exception):
            # Do the import here so that we don't entangle the GUI when using
            # command line functions
            from calibre.gui2.ui import get_gui
            info = get_gui().device_manager.get_current_device_information()
            if info is None:
                return ''
            try:
                if storage_location not in {'main', 'carda', 'cardb'}:
                    raise ValueError(
                         _('connected_device_name: invalid storage location "{}"').format(storage_location))
                info = info['info'][4]
                if storage_location not in info:
                    return ''
                return info[storage_location]['device_name']
            except Exception:
                traceback.print_exc()
                raise
        self.only_in_gui_error()


class BuiltinConnectedDeviceUUID(BuiltinFormatterFunction):
    name = 'connected_device_uuid'
    arg_count = 1
    category = GET_FROM_METADATA
    def __doc__getter__(self): return translate_ffml(
r'''
``connected_device_uuid(storage_location_key)`` -- if a device is connected then
return the device uuid (unique id), otherwise return the empty string.[/] Each
storage location on a device has a different uuid. The ``storage_location_key``
location names are ``'main'``, ``'carda'`` and ``'cardb'``. This function works
only in the GUI.
''')

    def evaluate(self, formatter, kwargs, mi, locals, storage_location):
        # We can't use get_database() here because we need the device manager.
        # In other words, the function really does need the GUI
        with suppress(Exception):
            # Do the import here so that we don't entangle the GUI when using
            # command line functions
            from calibre.gui2.ui import get_gui
            info = get_gui().device_manager.get_current_device_information()
            if info is None:
                return ''
            try:
                if storage_location not in {'main', 'carda', 'cardb'}:
                    raise ValueError(
                         _('connected_device_name: invalid storage location "{}"').format(storage_location))
                info = info['info'][4]
                if storage_location not in info:
                    return ''
                return info[storage_location]['device_store_uuid']
            except Exception:
                traceback.print_exc()
                raise
        self.only_in_gui_error()


class BuiltinCheckYesNo(BuiltinFormatterFunction):
    name = 'check_yes_no'
    arg_count = 4
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``check_yes_no(field_name, is_undefined, is_false, is_true)`` -- checks if the
value of the yes/no field named by the lookup name ``field_name`` is one of the
values specified by the parameters[/], returning ``'Yes'`` if a match is found
otherwise returning the empty string. Set the parameter ``is_undefined``,
``is_false``, or ``is_true`` to 1 (the number) to check that condition,
otherwise set it to 0.

Example: ``check_yes_no("#bool", 1, 0, 1)`` returns ``'Yes'`` if the yes/no field
``#bool`` is either True or undefined (neither True nor False).

More than one of ``is_undefined``, ``is_false``, or ``is_true`` can be set to 1.
''')

    def evaluate(self, formatter, kwargs, mi, locals, field, is_undefined, is_false, is_true):
        res = getattr(mi, field, None)
        # Missing fields will return None. Oh well, this lets it be used everywhere,
        # not just in the GUI.
        if res is None:
            if is_undefined == '1':
                return 'Yes'
            return ''
        if not isinstance(res, bool):
            raise ValueError(_('check_yes_no requires the field be a Yes/No custom column'))
        if is_false == '1' and not res:
            return 'Yes'
        if is_true == '1' and res:
            return 'Yes'
        return ''


class BuiltinRatingToStars(BuiltinFormatterFunction):
    name = 'rating_to_stars'
    arg_count = 2
    category = FORMATTING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``rating_to_stars(value, use_half_stars)`` -- Returns the ``value`` as string of star
(``{}``) characters.[/] The value must be a number between ``0`` and ``5``. Set
``use_half_stars`` to ``1`` if you want half star characters for fractional numbers
available with custom ratings columns.
''').format('★')

    def evaluate(self, formatter, kwargs, mi, locals, value, use_half_stars):
        if not value:
            return ''
        err_msg = translate_ffml('The rating must be a number between 0 and 5')
        try:
            v = float(value) * 2
        except Exception:
            raise ValueError(err_msg)
        if v < 0 or v > 10:
            raise ValueError(err_msg)
        from calibre.ebooks.metadata import rating_to_stars
        return rating_to_stars(v, use_half_stars == '1')


class BuiltinSwapAroundArticles(BuiltinFormatterFunction):
    name = 'swap_around_articles'
    arg_count = 2
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r''' ``swap_around_articles(value, separator)`` -- returns the ``value`` with
articles moved to the end, separated by a semicolon.[/] The ``value`` can be a
list, in which case each item in the list is processed. If the ``value`` is a
list then you must provide the ``separator``. If no ``separator`` is provided
or the separator is the empty string then the ``value`` is treated as being a
single value, not a list. The `articles` are those used by calibre to generate
the ``title_sort``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val, separator):
        if not val:
            return ''
        if not separator:
            return title_sort(val).replace(',', ';')
        result = []
        try:
            for v in [x.strip() for x in val.split(separator)]:
                result.append(title_sort(v).replace(',', ';'))
        except Exception:
            traceback.print_exc()
        return separator.join(sorted(result, key=sort_key))


class BuiltinArguments(BuiltinFormatterFunction):
    name = 'arguments'
    arg_count = -1
    category = OTHER
    def __doc__getter__(self): return translate_ffml(
r'''
``arguments(id[=expression] [, id[=expression]]*)`` -- Used in a stored
template to retrieve the arguments passed in the call.[/] It both declares and
initializes local variables with the supplied names, the ``id``s, making them
effectively parameters. The variables are positional; they get the value of
the argument given in the call in the same position. If the corresponding
argument is not provided in the call then ``arguments()`` assigns that variable
the provided default value. If there is no default value then the variable
is set to the empty string.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        # The arguments function is implemented in-line in the formatter
        raise NotImplementedError()


class BuiltinGlobals(BuiltinFormatterFunction):
    name = 'globals'
    arg_count = -1
    category = OTHER
    def __doc__getter__(self): return translate_ffml(
r'''
``globals(id[=expression] [, id[=expression]]*)`` -- Retrieves "global variables"
that can be passed into the formatter.[/] The name ``id`` is the name of the global
variable. It both declares and initializes local variables with the names of the
global variables passed in the ``id`` parameters. If the corresponding variable is not
provided in the globals then it assigns that variable the provided default
value. If there is no default value then the variable is set to the empty
string.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        # The globals function is implemented in-line in the formatter
        raise NotImplementedError()


class BuiltinSetGlobals(BuiltinFormatterFunction):
    name = 'set_globals'
    arg_count = -1
    category = OTHER
    def __doc__getter__(self): return translate_ffml(
r'''
``set_globals(id[=expression] [, id[=expression]]*)`` -- Sets `global
variables` that can be passed into the formatter.[/] The globals are given the name
of the ``id`` passed in. The value of the ``id`` is used unless an expression is
provided.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        # The globals function is implemented in-line in the formatter
        raise NotImplementedError()


class BuiltinFieldExists(BuiltinFormatterFunction):
    name = 'field_exists'
    arg_count = 1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``field_exists(lookup_name)`` -- checks if a field (column) with the lookup name
``lookup_name`` exists, returning ``'1'`` if so and the empty string if not.
''')

    def evaluate(self, formatter, kwargs, mi, locals, field_name):
        if field_name.lower() in mi.all_field_keys():
            return '1'
        return ''


class BuiltinCharacter(BuiltinFormatterFunction):
    name = 'character'
    arg_count = 1
    category = STRING_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``character(character_name)`` -- returns the character named by character_name.[/]
For example, ``character('newline')`` returns a newline character (``'\n'``).
The supported character names are ``newline``, ``return``, ``tab``, and
``backslash``. This function is used to put these characters into the output
of templates.
''')

    def evaluate(self, formatter, kwargs, mi, locals, character_name):
        # The globals function is implemented in-line in the formatter
        raise NotImplementedError()


class BuiltinToHex(BuiltinFormatterFunction):
    name = 'to_hex'
    arg_count = 1
    category = URL_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``to_hex(val)`` -- returns the string ``val`` encoded into hex.[/] This is useful
when constructing calibre URLs.
''')

    def evaluate(self, formatter, kwargs, mi, locals, val):
        return val.encode().hex()


class BuiltinUrlsFromIdentifiers(BuiltinFormatterFunction):
    name = 'urls_from_identifiers'
    arg_count = 2
    category = URL_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``urls_from_identifiers(identifiers, sort_results)`` -- given a comma-separated
list of ``identifiers``, where an ``identifier`` is a colon-separated pair of
values (``id_name:id_value``), returns a comma-separated list of HTML URLs
generated from the identifiers.[/] The list not sorted if ``sort_results`` is ``0``
(character or number), otherwise it is sorted alphabetically by the identifier
name. The URLs are generated in the same way as the built-in identifiers column
when shown in Book Details.
''')

    def evaluate(self, formatter, kwargs, mi, locals, identifiers, sort_results):
        from calibre.ebooks.metadata.sources.identify import urls_from_identifiers
        try:
            v = {}
            for id_ in identifiers.split(','):
                if id_:
                    pair = id_.split(':', maxsplit=1)
                    if len(pair) == 2:
                        l = pair[0].strip()
                        r = pair[1].strip()
                        if l and r:
                            v[l] = r
            urls = urls_from_identifiers(v, sort_results=str(sort_results) != '0')
            p = prepare_string_for_xml
            a = partial(prepare_string_for_xml, attribute=True)
            links = [f'<a href="{a(url)}" title="{a(id_typ)}:{a(id_val)}">{p(name)}</a>'
                for name, id_typ, id_val, url in urls]
            return ', '.join(links)
        except Exception as e:
            return str(e)


class BuiltinBookCount(BuiltinFormatterFunction):
    name = 'book_count'
    arg_count = 2
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``book_count(query, use_vl)`` -- returns the count of books found by searching
for ``query``.[/] If ``use_vl`` is ``0`` (zero) then virtual libraries are ignored.
This function and its companion ``book_values()`` are particularly useful in
template searches, supporting searches that combine information from many books
such as looking for series with only one book. It cannot be used in composite
columns unless the tweak ``allow_template_database_functions_in_composites`` is
set to True. It can be used only in the GUI.

For example this template search uses this function and its companion to find all series with only one book:
[LIST]
[*]Define a stored template (using :guilabel:`Preferences->Advanced->Template functions`)
named ``series_only_one_book`` (the name is arbitrary). The template
is:
[CODE]
program:
    vals = globals(vals='');
    if !vals then
        all_series = book_values('series', 'series:true', ',', 0);
        for series in all_series:
            if book_count('series:="' & series & '"', 0) == 1 then
                vals = list_join(',', vals, ',', series, ',')
            fi
        rof;
        set_globals(vals)
    fi;
    str_in_list(vals, ',', $series, 1, '')
[/CODE]
The first time the template runs (the first book checked) it stores the results
of the database lookups in a ``global`` template variable named ``vals``. These
results are used to check subsequent books without redoing the lookups.
[*] Use the stored template in a template search:
[CODE]
template:"program: series_only_one_book()#@#:n:1"
[/CODE]
Using a stored template instead of putting the template into the search
eliminates problems caused by the requirement to escape quotes in search
expressions.
[/LIST]
This function can be used only in the GUI and the content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, query, use_vl):
        from calibre.db.fields import rendering_composite_name
        if (not tweaks.get('allow_template_database_functions_in_composites', False) and
                formatter.global_vars.get(rendering_composite_name, None)):
            raise ValueError(_('The book_count() function cannot be used in a composite column'))
        db = self.get_database(mi, formatter=formatter)
        try:
            if use_vl == '0':
                # use the new_api search that doesn't use virtual libraries to let
                # the function work in content server icon rules.
                ids = db.new_api.search(query, None)
            else:
                ids = db.search_getting_ids(query, None, use_virtual_library=True)
            return str(len(ids))
        except Exception:
            traceback.print_exc()


class BuiltinBookValues(BuiltinFormatterFunction):
    name = 'book_values'
    arg_count = 4
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``book_values(column, query, sep, use_vl)`` -- returns a list of the unique
values contained in the column ``column`` (a lookup name), separated by ``sep``,
in the books found by searching for ``query``.[/] If ``use_vl`` is ``0`` (zero)
then virtual libraries are ignored. This function and its companion
``book_count()`` are particularly useful in template searches, supporting
searches that combine information from many books such as looking for series
with only one book. It cannot be used in composite columns unless the tweak
``allow_template_database_functions_in_composites`` is set to True. This function
can be used only in the GUI and the content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, column, query, sep, use_vl):
        from calibre.db.fields import rendering_composite_name
        if (not tweaks.get('allow_template_database_functions_in_composites', False) and
                formatter.global_vars.get(rendering_composite_name, None)):
            raise ValueError(_('The book_values() function cannot be used in a composite column'))
        db = self.get_database(mi, formatter=formatter)
        if column not in db.field_metadata:
            raise ValueError(_("The column {} doesn't exist").format(column))
        try:
            if use_vl == '0':
                ids = db.new_api.search(query, None)
            else:
                ids = db.search_getting_ids(query, None, use_virtual_library=True)
            s = set()
            for id_ in ids:
                f = db.new_api.get_proxy_metadata(id_).get(column, None)
                if isinstance(f, (tuple, list)):
                    s.update(f)
                elif f is not None:
                    s.add(str(f))
            return sep.join(s)
        except Exception as e:
            raise ValueError(e)


class BuiltinHasExtraFiles(BuiltinFormatterFunction):
    name = 'has_extra_files'
    arg_count = -1
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``has_extra_files([pattern])`` -- returns the count of extra files, otherwise ''
(the empty string).[/] If the optional parameter ``pattern`` (a regular expression)
is supplied then the list is filtered to files that match ``pattern`` before the
files are counted. The pattern match is case insensitive. See also the functions
:ref:`extra_file_names`, :ref:`extra_file_size` and :ref:`extra_file_modtime`.
This function can be used only in the GUI and the content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        if len(args) > 1:
            raise ValueError(_('Incorrect number of arguments for function {0}').format('has_extra_files'))
        pattern = args[0] if len(args) == 1 else None
        db = self.get_database(mi, formatter=formatter).new_api
        try:
            files = tuple(f.relpath.partition('/')[-1] for f in
                          db.list_extra_files(mi.id, use_cache=True, pattern=DATA_FILE_PATTERN))
            if pattern:
                r = re.compile(pattern, re.IGNORECASE)
                files = tuple(filter(r.search, files))
            return len(files) if len(files) > 0 else ''
        except Exception as e:
            traceback.print_exc()
            raise ValueError(e)


class BuiltinExtraFileNames(BuiltinFormatterFunction):
    name = 'extra_file_names'
    arg_count = -1
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``extra_file_names(sep [, pattern])`` -- returns a ``sep``-separated list of
extra files in the book's ``data/`` folder.[/] If the optional parameter
``pattern``, a regular expression, is supplied then the list is filtered to
files that match ``pattern``. The pattern match is case insensitive. See also
the functions :ref:`has_extra_files`, :ref:`extra_file_modtime` and
:ref:`extra_file_size`. This function can be used only in the GUI and the
content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, sep, *args):
        if len(args) > 1:
            raise ValueError(_('Incorrect number of arguments for function {0}').format('has_extra_files'))
        pattern = args[0] if len(args) == 1 else None
        db = self.get_database(mi, formatter=formatter).new_api
        try:
            files = tuple(f.relpath.partition('/')[-1] for f in
                          db.list_extra_files(mi.id, use_cache=True, pattern=DATA_FILE_PATTERN))
            if pattern:
                r = re.compile(pattern, re.IGNORECASE)
                files = tuple(filter(r.search, files))
            return sep.join(files)
        except Exception as e:
            traceback.print_exc()
            raise ValueError(e)


class BuiltinExtraFileSize(BuiltinFormatterFunction):
    name = 'extra_file_size'
    arg_count = 1
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``extra_file_size(file_name)`` -- returns the size in bytes of the extra file
``file_name`` in the book's ``data/`` folder if it exists, otherwise ``-1``.[/] See
also the functions :ref:`has_extra_files`, :ref:`extra_file_names` and
:ref:`extra_file_modtime`. This function can be used only in the GUI and the
content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, file_name):
        db = self.get_database(mi, formatter=formatter).new_api
        try:
            q = posixpath.join(DATA_DIR_NAME, file_name)
            for f in db.list_extra_files(mi.id, use_cache=True, pattern=DATA_FILE_PATTERN):
                if f.relpath == q:
                    return str(f.stat_result.st_size)
            return str(-1)
        except Exception as e:
            traceback.print_exc()
            raise ValueError(e)


class BuiltinExtraFileModtime(BuiltinFormatterFunction):
    name = 'extra_file_modtime'
    arg_count = 2
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``extra_file_modtime(file_name, format_string)`` -- returns the modification
time of the extra file ``file_name`` in the book's ``data/`` folder[/] if it
exists, otherwise ``-1``. The modtime is formatted according to
``format_string`` (see :ref:`format_date` for details). If ``format_string`` is
the empty string, returns the modtime as the floating point number of seconds
since the epoch.  See also the functions :ref:`has_extra_files`,
:ref:`extra_file_names` and :ref:`extra_file_size`. The epoch is OS dependent.
This function can be used only in the GUI and the content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, file_name, format_string):
        db = self.get_database(mi, formatter=formatter).new_api
        try:
            q = posixpath.join(DATA_DIR_NAME, file_name)
            for f in db.list_extra_files(mi.id, use_cache=True, pattern=DATA_FILE_PATTERN):
                if f.relpath == q:
                    val = f.stat_result.st_mtime
                    if format_string:
                        return format_date(datetime.fromtimestamp(val), format_string)
                    return str(val)
            return str(1.0)
        except Exception as e:
            traceback.print_exc()
            raise ValueError(e)


class BuiltinGetNote(BuiltinFormatterFunction):
    name = 'get_note'
    arg_count = 3
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``get_note(field_name, field_value, plain_text)`` -- fetch the note for field
``field_name`` with value ``field_value``.[/] If ``plain_text`` is empty, return the
note's HTML including images. If ``plain_text`` is ``1`` (or ``'1'``), return the
note's plain text. If the note doesn't exist, return the empty string in both
cases. Example:
[LIST]
[*]Return the HTML of the note attached to the tag `Fiction`:
[CODE]
program:
    get_note('tags', 'Fiction', '')
[/CODE]
[*]Return the plain text of the note attached to the author `Isaac Asimov`:
[CODE]
program:
    get_note('authors', 'Isaac Asimov', 1)
[/CODE]
[/LIST]
This function works only in the GUI and the content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, field_name, field_value, plain_text):
        db = self.get_database(mi, formatter=formatter).new_api
        try:
            note = None
            item_id = db.get_item_id(field_name, field_value, case_sensitive=True)
            if item_id is not None:
                note = db.notes_data_for(field_name, item_id)
                if note is not None:
                    if plain_text == '1':
                        note = note['searchable_text'].partition('\n')[2]
                    else:
                        from lxml import html

                        from calibre.db.notes.exim import expand_note_resources, parse_html
                        # Return the full HTML of the note, including all images
                        # as data: URLs. Reason: non-exported note html contains
                        # "calres://" URLs for images. These images won't render
                        # outside the context of the library where the note
                        # "lives". For example, they don't work in book jackets
                        # and book details from a different library. They also
                        # don't work in tooltips.

                        # This code depends on the note being wrapped in <body>
                        # tags by parse_html. The body is changed to a <div>.
                        # That means we often end up with <div><div> or some
                        # such, but that is OK
                        root = parse_html(note['doc'])
                        # There should be only one <body>
                        root = root.xpath('//body')[0]
                        # Change the body to a div
                        root.tag = 'div'
                        # Expand all the resources in the note
                        root = expand_note_resources(root, db.get_notes_resource)
                        note = html.tostring(root, encoding='unicode')
            return '' if note is None else note
        except Exception as e:
            traceback.print_exc()
            raise ValueError(e)


class BuiltinHasNote(BuiltinFormatterFunction):
    name = 'has_note'
    arg_count = 2
    category = DB_FUNCS
    def __doc__getter__(self): return translate_ffml(
r'''
``has_note(field_name, field_value)``. Check if a field has a note.[/]
This function has two variants:
[LIST]
[*]if ``field_value`` is not ``''`` (the empty string) return ``'1'`` if the
value ``field_value`` in the field ``field_name`` has a note, otherwise ``''``.

Example: ``has_note('tags', 'Fiction')`` returns ``'1'`` if the tag ``fiction`` has an attached note, otherwise ``''``.

[*]If ``field_value`` is ``''`` then return a list of values in ``field_name``
that have a note. If no item in the field has a note, return ``''``.  This
variant is useful for showing column icons if any value in the field has a note,
rather than a specific value.

Example: ``has_note('authors', '')``   returns a list of authors that have notes, or
``''`` if no author has a note.
[/LIST]

You can test if all the values in ``field_name`` have a note by comparing the
list length of this function's return value against the list length of the
values in ``field_name``. Example:
[CODE]
    list_count(has_note('authors', ''), '&') ==# list_count_field('authors')
[/CODE]
This function works only in the GUI and the content server.
''')

    def evaluate(self, formatter, kwargs, mi, locals, field_name, field_value):
        db = self.get_database(mi, formatter=formatter).new_api
        if field_value:
            note = None
            try:
                item_id = db.get_item_id(field_name, field_value, case_sensitive=True)
                if item_id is not None:
                    note = db.notes_data_for(field_name, item_id)
            except Exception as e:
                traceback.print_exc()
                raise ValueError(str(e))
            return '1' if note is not None else ''
        try:
            notes_for_book = db.items_with_notes_in_book(mi.id)
            values = list(notes_for_book.get(field_name, {}).values())
            return db.field_metadata[field_name]['is_multiple'].get('list_to_ui', ', ').join(values)
        except Exception as e:
            traceback.print_exc()
            raise ValueError(str(e))


class BuiltinIsDarkMode(BuiltinFormatterFunction):
    name = 'is_dark_mode'
    arg_count = 0
    category = OTHER
    def __doc__getter__(self): return translate_ffml(
r'''
``is_dark_mode()`` -- returns ``'1'`` if calibre is running in dark mode, ``''``
(the empty string) otherwise.[/] This function can be used in advanced color and
icon rules to choose different colors/icons according to the mode. Example:
[CODE]
   if is_dark_mode() then 'dark.png' else 'light.png' fi
[/CODE]
''')

    def evaluate(self, formatter, kwargs, mi, locals):
        try:
            # Import this here so that Qt isn't referenced unless this function is used.
            from calibre.gui2 import is_dark_theme
            return '1' if is_dark_theme() else ''
        except Exception:
            only_in_gui_error('is_dark_mode')


class BuiltinFieldListCount(BuiltinFormatterFunction):
    name = 'list_count_field'
    arg_count = 0
    category = LIST_MANIPULATION
    def __doc__getter__(self): return translate_ffml(
r'''
``list_count_field(lookup_name)``-- returns the count of items in the field with
the lookup name ``lookup_name``.[/] The field must be multi-valued such as
``authors`` or ``tags``, otherwise the function raises an error. This function
is much faster than ``list_count()`` because it operates directly on calibre
data without converting it to a string first. Example: ``list_count_field('tags')``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        # The globals function is implemented in-line in the formatter
        raise NotImplementedError()


class BuiltinMakeUrl(BuiltinFormatterFunction):
    name = 'make_url'
    arg_count = -1
    category = URL_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``make_url(path, [query_name, query_value]+)`` -- this function is the easiest way
to construct a query URL. It uses a ``path``, the web site and page you want to
query, and ``query_name``, ``query_value`` pairs from which the query is built.
In general, the ``query_value`` must be URL-encoded. With this function it is always
encoded and spaces are always replaced with ``'+'`` signs.[/]

At least one ``query_name, query_value`` pair must be provided.

Example: constructing a Wikipedia search URL for the author `{0}`:
[CODE]
make_url('https://en.wikipedia.org/w/index.php', 'search', '{0}')
[/CODE]
returns
[CODE]
https://en.wikipedia.org/w/index.php?search=Niccol%C3%B2+Machiavelli
[/CODE]

If you are writing a custom column book details URL template then use ``$item_name`` or
``field('item_name')`` to obtain the value of the field that was clicked on.
Example: if `{0}` was clicked then you can construct the URL using:
[CODE]
make_url('https://en.wikipedia.org/w/index.php', 'search', $item_name)
[/CODE]

See also the functions :ref:`make_url_extended`, :ref:`query_string` and :ref:`encode_for_url`.
''').format('Niccolò Machiavelli')  # not translated ans gettext wants pure ascii msgid

    def evaluate(self, formatter, kwargs, mi, locals, path, *args):
        if (len(args) % 2) != 0:
            raise ValueError(_('{} requires an odd number of arguments').format('make_url'))
        if len(args) < 2:
            raise ValueError(_('{} requires at least 3 arguments').format('make_url'))
        query_args = []
        for i in range(0, len(args), 2):
            query_args.append(f'{args[i]}={qquote(args[i+1].strip())}')
        return f'{path}?{"&".join(query_args)}'


class BuiltinMakeUrlExtended(BuiltinFormatterFunction):
    name = 'make_url_extended'
    arg_count = -1
    category = URL_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``make_url_extended(...)`` -- this function is similar to :ref:`make_url` but
gives you more control over the URL components. The components of a URL are

[B]scheme[/B]:://[B]authority[/B]/[B]path[/B]?[B]query string[/B].

See [URL href="https://en.wikipedia.org/wiki/URL"]Uniform Resource Locator[/URL] on Wikipedia for more detail.

The function has two variants:
[CODE]
make_url_extended(scheme, authority, path, [query_name, query_value]+)
[/CODE]
and
[CODE]
make_url_extended(scheme, authority, path, query_string)
[/CODE]
[/]
This function returns a URL constructed from the ``scheme``, ``authority``, ``path``,
and either the ``query_string`` or a query string constructed from the query argument pairs.
The ``authority`` can be empty, which is the case for ``calibre`` scheme URLs.
You must supply either a ``query_string`` or at least one ``query_name, query_value`` pair.
If you supply ``query_string`` and it is empty then the resulting URL will not have a query string section.

Example 1: constructing a Wikipedia search URL for the author `{0}`:
[CODE]
make_url_extended('https', 'en.wikipedia.org', '/w/index.php', 'search', '{0}')
[/CODE]
returns
[CODE]
https://en.wikipedia.org/w/index.php?search=Niccol%C3%B2+Machiavelli
[/CODE]

See the :ref:`query_string` function for an example using ``make_url_extended()`` with a ``query_string``.

If you are writing a custom column book details URL template then use ``$item_name`` or
``field('item_name')`` to obtain the value of the field that was clicked on.
Example: if `{0}` was clicked on then you can construct the URL using :
[CODE]
make_url_extended('https', 'en.wikipedia.org', '/w/index.php', 'search', $item_name')
[/CODE]

See also the functions :ref:`make_url`, :ref:`query_string` and :ref:`encode_for_url`.
''').format('Niccolò Machiavelli')  # not translated as gettext wants pure ASCII msgid

    def evaluate(self, formatter, kwargs, mi, locals, scheme, authority, path, *args):
        if len(args) != 1:
            if (len(args) % 2) != 0:
                raise ValueError(_('{} requires an odd number of arguments').format('make_url_extended'))
            if len(args) < 2:
                raise ValueError(_('{} requires at least 5 arguments').format('make_url_extended'))
            query_args = []
            for i in range(0, len(args), 2):
                query_args.append(f'{args[i]}={qquote(args[i+1].strip())}')
            qs = '&'.join(query_args)
        else:
            qs = args[0]
        if qs:
            qs = '?' + qs
        return (f"{scheme}://{authority}{'/' if authority else ''}"
                f"{path[1:] if path.startswith('/') else path}{qs}")


class BuiltinQueryString(BuiltinFormatterFunction):
    name = 'query_string'
    arg_count = -1
    category = URL_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``query_string([query_name, query_value, how_to_encode]+)``-- returns a URL query string
constructed from the ``query_name, query_value, how_to_encode`` triads.
A query string is a series of items where each item looks like ``query_name=query_value``
where ``query_value`` is URL-encoded as instructed. The query items are separated by
``'&'`` (ampersand) characters.[/]

If ``how_to_encode`` is ``0`` then ``query_value`` is encoded and spaces are replaced
with ``'+'`` (plus) signs. If ``how_to_encode`` is ``1`` then ``query_value`` is
encoded with spaces replaced by ``%20``. If ``how_to_encode`` is ``2`` then ``query_value``
is returned unchanged; no encoding is done and spaces are not replaced. If you want
``query_value`` not to be encoded but spaces to be replaced then use the :ref:`re`
function, as in ``re($series, ' ', '%20')``

You use this function if you need specific control over how the parts of the
query string are constructed. You could then use the resultingquery string in
:ref:`make_url_extended`, as in
[CODE]
make_url_extended(
       'https', 'your_host', 'your_path',
       query_string('encoded', '{0}', 0, 'unencoded', '{0}', 2))
[/CODE]
giving you
[CODE]
https://your_host/your_path?encoded=Hendrik+B%C3%A4%C3%9Fler&unencoded={0}
[/CODE]

You must have at least one ``query_name, query_value, how_to_encode`` triad, but can
have as many as you wish.

The returned value is a URL query string with all the specified items, for example:
``name1=val1[&nameN=valN]*``. Note that the ``'?'`` `path` / `query string` separator
is not included in the returned result.

If you are writing a custom column book details URL template then use ``$item_name`` or
``field('item_name')`` to obtain the unencoded value of the field that was clicked.
You also have ``item_value_quoted`` where the value is already encoded with plus signs
replacing spaces, and ``item_value_no_plus`` where the value is already encoded
with ``%20`` replacing spaces.

See also the functions :ref:`make_url`, :ref:`make_url_extended` and :ref:`encode_for_url`.
''').format('Hendrik Bäßler')

    def evaluate(self, formatter, kwargs, mi, locals, *args):
        if (len(args) % 3) != 0 or len(args) < 3:
            raise ValueError(_('{} requires at least one group of 3 arguments').format('query_string'))
        funcs = [
            partial(qquote, use_plus=True),
            partial(qquote, use_plus=False),
            lambda x:x,
        ]
        query_args = []
        for i in range(0, len(args), 3):
            if (f := args[i+2]) not in ('0', '1', '2'):
                raise ValueError(
                    _('In {} the third argument of a group must be 0, 1, or 2, not {}').format('query_string', f))
            query_args.append(f'{args[i]}={funcs[int(f)](args[i+1].strip())}')
        return '&'.join(query_args)


class BuiltinEncodeForURL(BuiltinFormatterFunction):
    name = 'encode_for_url'
    arg_count = 2
    category = URL_FUNCTIONS
    def __doc__getter__(self): return translate_ffml(
r'''
``encode_for_url(value, use_plus)`` -- returns the ``value`` encoded for use in a URL as
specified by ``use_plus``. The value is first URL-encoded. Next, if ``use_plus`` is ``0`` then
spaces are replaced by ``'+'`` (plus) signs. If it is ``1`` then spaces are replaced by ``%20``.[/]

If you do not want the value to be encoding but to have spaces replaced then use the
:ref:`re` function, as in ``re($series, ' ', '%20')``

See also the functions :ref:`make_url`, :ref:`make_url_extended` and :ref:`query_string`.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value, use_plus):
        if use_plus not in ('0', '1'):
            raise ValueError(
                _('In {} the second argument must be 0, or 1, not {}').format('quote_for_url', use_plus))
        return qquote(value, use_plus=use_plus=='0')


class BuiltinFormatDuration(BuiltinFormatterFunction):
    name = 'format_duration'
    arg_count = -1
    category = FORMATTING_VALUES
    def __doc__getter__(self): return translate_ffml(
r'''
``format_duration(value, template, [largest_unit])`` -- format the value, a number
of seconds, into a string showing weeks, days, hours, minutes, and seconds. If
the value is a float then it is rounded to the nearest integer.[/]  You choose
how to format the value using a template consisting of value selectors
surrounded by ``[`` and ``]`` characters. The selectors are:
[LIST]
[*]``[w]``: weeks
[*]``[d]``: days
[*]``[h]``: hours
[*]``[m]``: minutes
[*]``[s]``: seconds
[/LIST]
You can put arbitrary text between selectors.

The following examples use a duration of 2 days (172,800 seconds) 1 hour (3,600 seconds)
and 20 seconds, which totals to 176,420 seconds.
[LIST]
[*]``format_duration(176420, '[d][h][m][s]')`` will return the value ``2d 1h 0m 20s``.
[*]``format_duration(176420, '[h][m][s]')`` will return the value ``49h 0m 20s``.
[*]``format_duration(176420, 'Your reading time is [d][h][m][s]')`` returns the value
``Your reading time is 49h 0m 20s``.
[*]``format_duration(176420, '[w][d][h][m][s]')`` will return the value ``2d 1h 0m 20s``.
Note that the zero weeks value is not returned.
[/LIST]
If you want to see zero values for items such as weeks in the above example,
use an uppercase selector. For example, the following uses ``'W'`` to show zero weeks:

``format_duration(176420, '[W][d][h][m][s]')`` returns ``0w 2d 1h 0m 20s``.

By default the text following a value is the selector followed by a space.
You can change that to whatever text you want. The format for a selector with
your text is the selector followed by a colon followed by text
segments separated by ``'|'`` characters. You must include any space characters
you want in the output.

You can provide from one to three text segments.
[LIST]
[*]If you provide one segment, as in ``[w: weeks ]`` then that segment is used for all values.
[*]If you provide two segments, as in ``[w: weeks | week ]`` then the first segment
is used for 0 and more than 1. The second segment is used for 1.
[*]If you provide three segments, as in ``[w: weeks | week | weeks ]`` then the first
segment is used for 0, the second segment is used for 1, and the third segment is used for
more than 1.
[/LIST]
The second form is equivalent to the third form in many languages.

For example, the selector:
[LIST]
[*]``[w: weeks | week | weeks ]`` produces ``'0 weeks '``, ``'1 week '``, or ``'2 weeks '``.
[*]``[w: weeks | week ]`` produces ``'0 weeks '``, ``'1 week '``, or ``'2 weeks '``.
[*]``[w: weeks ]`` produces ``0 weeks '``, ``1 weeks '``, or ``2 weeks '``.
[/LIST]

The optional ``largest_unit`` parameter specifies the largest of weeks, days, hours, minutes,
and seconds that will be produced by the template. It must be one of the value selectors.
This can be useful to truncate a value.

``format_duration(176420, '[h][m][s]', 'd')`` will return the value ``1h 0m 20s`` instead of ``49h 0m 20s``.
''')

    def evaluate(self, formatter, kwargs, mi, locals, value, template, largest_unit=''):
        if largest_unit not in 'wdhms':
            raise ValueError(_('the {0} parameter must be one of {1}').format('largest_unit', 'wdhms'))

        pat = re.compile(r'\[(.)(:(.*?))?\]')

        if not largest_unit:
            highest_index = 0
            for m in pat.finditer(template):
                try:
                    # We know that m.group(1) is a single character so the only
                    # exception possible is that the character is not in the string
                    dex = 'smhdw'.index(m.group(1).lower())
                    highest_index = dex if dex > highest_index else highest_index
                except Exception:
                    raise ValueError(_('The {} format specifier is not valid').format(m.group()))
            largest_unit = 'smhdw'[highest_index]

        int_val = remainder = round(float(value)) if value else 0
        weeks,remainder = divmod(remainder, 60*60*24*7) if largest_unit == 'w' else (-1,remainder)
        days,remainder = divmod(remainder, 60*60*24) if largest_unit in 'wd' else (-1,remainder)
        hours,remainder = divmod(remainder, 60*60) if largest_unit in 'wdh' else (-1,remainder)
        minutes,remainder = divmod(remainder, 60) if largest_unit in 'wdhm' else (-1,remainder)
        seconds = remainder

        def repl(mo):
            fmt_char = mo.group(1)
            suffixes = mo.group(3)
            if suffixes is None:
                zero_suffix = one_suffix = more_suffix = fmt_char.lower() + ' '
            else:
                suffixes = re.split(r'\|', suffixes)
                match len(suffixes):
                    case 1:
                        zero_suffix = one_suffix = more_suffix = suffixes[0]
                    case 2:
                        zero_suffix = more_suffix = suffixes[0]
                        one_suffix = suffixes[1]
                    case 3:
                        zero_suffix = suffixes[0]
                        one_suffix = suffixes[1]
                        more_suffix = suffixes[2]
                    case _:
                        raise ValueError(_('The group {} has too many suffixes').format(fmt_char))
                        zero_suffix = one_suffix = more_suffix = '@@too many suffixes@@'

            def val_with_suffix(val, test_val):
                match val:
                    case -1:
                        return ''
                    case 0 if fmt_char.islower() and int_val < test_val:
                        return ''
                    case 0:
                        return str(val) + zero_suffix
                    case 1:
                        return str(val) + one_suffix
                    case _:
                        return str(val) + more_suffix

            match fmt_char.lower():
                case 'w':
                    return val_with_suffix(weeks, 60*60*24*7)
                case 'd':
                    return val_with_suffix(days, 60*60*24)
                case 'h':
                    return val_with_suffix(hours, 60*60)
                case 'm':
                    return val_with_suffix(minutes, 60)
                case 's':
                    return val_with_suffix(seconds, -1)
                case _:
                    raise ValueError(_('The {} format specifier is not valid').format(fmt_char))

        return pat.sub(repl, template)


_formatter_builtins = [
    BuiltinAdd(), BuiltinAnd(), BuiltinApproximateFormats(), BuiltinArguments(),
    BuiltinAssign(),
    BuiltinAuthorLinks(), BuiltinAuthorSorts(), BuiltinBookCount(),
    BuiltinBookValues(), BuiltinBooksize(),
    BuiltinCapitalize(), BuiltinCharacter(), BuiltinCheckYesNo(), BuiltinCeiling(),
    BuiltinCmp(), BuiltinConnectedDeviceName(), BuiltinConnectedDeviceUUID(), BuiltinContains(),
    BuiltinCount(), BuiltinCurrentLibraryName(), BuiltinCurrentLibraryPath(),
    BuiltinCurrentVirtualLibraryName(), BuiltinDateArithmetic(),
    BuiltinDaysBetween(), BuiltinDivide(), BuiltinEncodeForURL(), BuiltinEval(),
    BuiltinExtraFileNames(), BuiltinExtraFileSize(), BuiltinExtraFileModtime(),
    BuiltinFieldListCount(), BuiltinFirstNonEmpty(), BuiltinField(), BuiltinFieldExists(),
    BuiltinFinishFormatting(), BuiltinFirstMatchingCmp(), BuiltinFloor(),
    BuiltinFormatDate(), BuiltinFormatDateField(), BuiltinFormatDuration(), BuiltinFormatNumber(),
    BuiltinFormatsModtimes(),BuiltinFormatsPaths(), BuiltinFormatsPathSegments(),
    BuiltinFormatsSizes(), BuiltinFractionalPart(),BuiltinGetLink(),
    BuiltinGetNote(), BuiltinGlobals(), BuiltinHasCover(), BuiltinHasExtraFiles(),
    BuiltinHasNote(), BuiltinHumanReadable(), BuiltinIdentifierInList(),
    BuiltinIfempty(), BuiltinIsDarkMode(), BuiltinLanguageCodes(), BuiltinLanguageStrings(),
    BuiltinInList(), BuiltinIsMarked(), BuiltinListCountMatching(),
    BuiltinListDifference(), BuiltinListEquals(), BuiltinListIntersection(),
    BuiltinListitem(), BuiltinListJoin(), BuiltinListRe(),
    BuiltinListReGroup(), BuiltinListRemoveDuplicates(), BuiltinListSort(),
    BuiltinListSplit(), BuiltinListUnion(),BuiltinLookup(),
    BuiltinLowercase(), BuiltinMakeUrl(), BuiltinMakeUrlExtended(), BuiltinMod(),
    BuiltinMultiply(), BuiltinNot(), BuiltinOndevice(),
    BuiltinOr(), BuiltinPrint(), BuiltinQueryString(), BuiltinRatingToStars(),
    BuiltinRange(), BuiltinRawField(), BuiltinRawList(),
    BuiltinRe(), BuiltinReGroup(), BuiltinRound(), BuiltinSelect(), BuiltinSeriesSort(),
    BuiltinSetGlobals(), BuiltinShorten(), BuiltinStrcat(), BuiltinStrcatMax(),
    BuiltinStrcmp(), BuiltinStrcmpcase(), BuiltinStrInList(), BuiltinStrlen(), BuiltinSubitems(),
    BuiltinSublist(),BuiltinSubstr(), BuiltinSubtract(), BuiltinSwapAroundArticles(),
    BuiltinSwapAroundComma(), BuiltinSwitch(), BuiltinSwitchIf(),
    BuiltinTemplate(), BuiltinTest(), BuiltinTitlecase(), BuiltinToday(),
    BuiltinToHex(), BuiltinTransliterate(), BuiltinUppercase(), BuiltinUrlsFromIdentifiers(),
    BuiltinUserCategories(), BuiltinVirtualLibraries(), BuiltinAnnotationCount()
]


class FormatterUserFunction(FormatterFunction):

    def __init__(self, name, doc, arg_count, program_text, object_type):
        self.object_type = object_type
        self.name = name
        self.user_doc = doc
        self.arg_count = arg_count
        self._cached_program_text = program_text or ''
        self.cached_compiled_text = None
        # Keep this for external code compatibility. Set it to True if we have a
        # python template function, otherwise false. This might break something
        # if the code depends on stored templates being in GPM.
        self.is_python = True if object_type is StoredObjectType.PythonFunction else False

    def to_pref(self):
        return [self.name, self.doc, self.arg_count, self.program_text]

    def __doc__getter__(self):
        return self.user_doc


tabs = re.compile(r'^\t*')


def function_object_type(thing):
    # 'thing' can be a preference instance, program text, or an already-compiled function
    if isinstance(thing, FormatterUserFunction):
        return thing.object_type
    if isinstance(thing, list):
        text = thing[3]
    else:
        text = thing
    if text.startswith('def'):
        return StoredObjectType.PythonFunction
    if text.startswith('program'):
        return StoredObjectType.StoredGPMTemplate
    if text.startswith('python'):
        return StoredObjectType.StoredPythonTemplate
    raise ValueError('Unknown program type in formatter function pref')


def function_pref_name(pref):
    return pref[0]


def compile_user_function(name, doc, arg_count, eval_func):
    typ = function_object_type(eval_func)
    if typ is not StoredObjectType.PythonFunction:
        return FormatterUserFunction(name, doc, arg_count, eval_func, typ)

    def replace_func(mo):
        return mo.group().replace('\t', '    ')

    func = '    ' + '\n    '.join([tabs.sub(replace_func, line)
                                   for line in eval_func.splitlines()])
    prog = '''
from calibre.utils.formatter_functions import FormatterUserFunction
from calibre.utils.formatter_functions import formatter_functions
class UserFunction(FormatterUserFunction):
''' + func
    locals_ = {}
    if DEBUG and tweaks.get('enable_template_debug_printing', False):
        print(prog)
    exec(prog, locals_)
    cls = locals_['UserFunction'](name, doc, arg_count, eval_func, typ)
    return cls


def compile_user_template_functions(funcs):
    compiled_funcs = {}
    for func in funcs:
        try:
            # Force a name conflict to test the logic
            # if func[0] == 'myFunc2':
            #     func[0] = 'myFunc3'

            # Compile the function so that the tab processing is done on the
            # source. This helps ensure that if the function already is defined
            # then white space differences don't cause them to compare differently

            cls = compile_user_function(*func)
            cls.object_type = function_object_type(func)
            compiled_funcs[cls.name] = cls
        except Exception:
            try:
                func_name = func[0]
            except Exception:
                func_name = 'Unknown'
            prints(f'**** Compilation errors in user template function "{func_name}" ****')
            traceback.print_exc(limit=10)
            prints(f'**** End compilation errors in {func_name} "****"')
    return compiled_funcs


def load_user_template_functions(library_uuid, funcs, precompiled_user_functions=None):
    unload_user_template_functions(library_uuid)
    if precompiled_user_functions:
        compiled_funcs = precompiled_user_functions
    else:
        compiled_funcs = compile_user_template_functions(funcs)
    formatter_functions().register_functions(library_uuid, list(compiled_funcs.values()))


def unload_user_template_functions(library_uuid):
    formatter_functions().unregister_functions(library_uuid)
