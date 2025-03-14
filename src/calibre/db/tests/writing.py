#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import os
from collections import namedtuple
from functools import partial
from io import BytesIO

from calibre.db.backend import FTSQueryError
from calibre.db.constants import RESOURCE_URL_SCHEME
from calibre.db.tests.base import IMG, BaseTest
from calibre.ebooks.metadata import author_to_author_sort, title_sort
from calibre.ebooks.metadata.book.base import Metadata
from calibre.utils.date import UNDEFINED_DATE
from polyglot.builtins import iteritems, itervalues


class WritingTest(BaseTest):

    # Utils {{{
    def create_getter(self, name, getter=None):
        if getter is None:
            if name.endswith('_index'):
                def ans(db):
                    return partial(db.get_custom_extra, index_is_id=True, label=name[1:].replace('_index', ''))
            else:
                def ans(db):
                    return partial(db.get_custom, label=name[1:], index_is_id=True)
        else:
            def ans(db):
                return partial(getattr(db, getter), index_is_id=True)
        return ans

    def create_setter(self, name, setter=None):
        if setter is None:
            def ans(db):
                return partial(db.set_custom, label=name[1:], commit=True)
        else:
            def ans(db):
                return partial(getattr(db, setter), commit=True)
        return ans

    def create_test(self, name, vals, getter=None, setter=None):
        T = namedtuple('Test', 'name vals getter setter')
        return T(name, vals, self.create_getter(name, getter),
                 self.create_setter(name, setter))

    def run_tests(self, tests):
        results = {}
        for test in tests:
            results[test] = []
            for val in test.vals:
                cl = self.cloned_library
                cache = self.init_cache(cl)
                cache.set_field(test.name, {1: val})
                cached_res = cache.field_for(test.name, 1)
                del cache
                db = self.init_old(cl)
                getter = test.getter(db)
                sqlite_res = getter(1)
                if test.name.endswith('_index'):
                    val = float(val) if val is not None else 1.0
                    self.assertEqual(sqlite_res, val,
                        f'Failed setting for {test.name} with value {val!r}, sqlite value not the same. val: {val!r} != sqlite_val: {sqlite_res!r}')
                else:
                    test.setter(db)(1, val)
                    old_cached_res = getter(1)
                    self.assertEqual(old_cached_res, cached_res,
                        f'Failed setting for {test.name} with value {val!r}, cached value not the same. Old: {old_cached_res!r} != New: {cached_res!r}')
                    db.refresh()
                    old_sqlite_res = getter(1)
                    self.assertEqual(old_sqlite_res, sqlite_res,
                        f'Failed setting for {test.name}, sqlite value not the same: {old_sqlite_res!r} != {sqlite_res!r}')
                del db
    # }}}

    def test_one_one(self):  # {{{
        'Test setting of values in one-one fields'
        tests = [self.create_test('#yesno', (True, False, 'true', 'false', None))]
        for name, getter, setter in (
            ('#series_index', None, None),
            ('series_index', 'series_index', 'set_series_index'),
            ('#float', None, None),
        ):
            vals = ['1.5', None, 0, 1.0]
            tests.append(self.create_test(name, tuple(vals), getter, setter))

        for name, getter, setter in (
            ('pubdate', 'pubdate', 'set_pubdate'),
            ('timestamp', 'timestamp', 'set_timestamp'),
            ('#date', None, None),
        ):
            tests.append(self.create_test(
                name, ('2011-1-12', UNDEFINED_DATE, None), getter, setter))

        for name, getter, setter in (
            ('title', 'title', 'set_title'),
            ('uuid', 'uuid', 'set_uuid'),
            ('author_sort', 'author_sort', 'set_author_sort'),
            ('sort', 'title_sort', 'set_title_sort'),
            ('#comments', None, None),
            ('comments', 'comments', 'set_comment'),
        ):
            vals = ['something', None]
            if name not in {'comments', '#comments'}:
                # Setting text column to '' returns None in the new backend
                # and '' in the old. I think None is more correct.
                vals.append('')
            if name == 'comments':
                # Again new behavior of deleting comment rather than setting
                # empty string is more correct.
                vals.remove(None)
            tests.append(self.create_test(name, tuple(vals), getter, setter))

        self.run_tests(tests)
    # }}}

    def test_many_one_basic(self):  # {{{
        'Test the different code paths for writing to a many-one field'
        cl = self.cloned_library
        cache = self.init_cache(cl)
        f = cache.fields['publisher']
        item_ids = {f.ids_for_book(1)[0], f.ids_for_book(2)[0]}
        val = 'Changed'
        self.assertEqual(cache.set_field('publisher', {1:val, 2:val}), {1, 2})
        cache2 = self.init_cache(cl)
        for book_id in (1, 2):
            for c in (cache, cache2):
                self.assertEqual(c.field_for('publisher', book_id), val)
                self.assertFalse(item_ids.intersection(set(c.fields['publisher'].table.id_map)))
        del cache2
        self.assertFalse(cache.set_field('publisher', {1:val, 2:val}))
        val = val.lower()
        self.assertFalse(cache.set_field('publisher', {1:val, 2:val},
                                         allow_case_change=False))
        self.assertEqual(cache.set_field('publisher', {1:val, 2:val}), {1, 2})
        cache2 = self.init_cache(cl)
        for book_id in (1, 2):
            for c in (cache, cache2):
                self.assertEqual(c.field_for('publisher', book_id), val)
        del cache2
        self.assertEqual(cache.set_field('publisher', {1:'new', 2:'New'}), {1, 2})
        self.assertEqual(cache.field_for('publisher', 1).lower(), 'new')
        self.assertEqual(cache.field_for('publisher', 2).lower(), 'new')
        self.assertEqual(cache.set_field('publisher', {1:None, 2:'NEW'}), {1, 2})
        self.assertEqual(len(f.table.id_map), 1)
        self.assertEqual(cache.set_field('publisher', {2:None}), {2})
        self.assertEqual(len(f.table.id_map), 0)
        cache2 = self.init_cache(cl)
        self.assertEqual(len(cache2.fields['publisher'].table.id_map), 0)
        del cache2
        self.assertEqual(cache.set_field('publisher', {1:'one', 2:'two',
                                                       3:'three'}), {1, 2, 3})
        self.assertEqual(cache.set_field('publisher', {1:''}), {1})
        self.assertEqual(cache.set_field('publisher', {1:'two'}), {1})
        self.assertEqual(tuple(map(f.for_book, (1,2,3))), ('two', 'two', 'three'))
        self.assertEqual(cache.set_field('publisher', {1:'Two'}), {1, 2})
        cache2 = self.init_cache(cl)
        self.assertEqual(tuple(map(f.for_book, (1,2,3))), ('Two', 'Two', 'three'))
        del cache2

        # Enum
        self.assertFalse(cache.set_field('#enum', {1:'Not allowed'}))
        self.assertEqual(cache.set_field('#enum', {1:'One', 2:'One', 3:'Three'}), {1, 3})
        self.assertEqual(cache.set_field('#enum', {1:None}), {1})
        cache2 = self.init_cache(cl)
        for c in (cache, cache2):
            for i, val in iteritems({1:None, 2:'One', 3:'Three'}):
                self.assertEqual(c.field_for('#enum', i), val)
        del cache2

        # Rating
        self.assertFalse(cache.set_field('rating', {1:6, 2:4}))
        self.assertEqual(cache.set_field('rating', {1:0, 3:2}), {1, 3})
        self.assertEqual(cache.set_field('#rating', {1:None, 2:4, 3:8}), {1, 2, 3})
        cache2 = self.init_cache(cl)
        for c in (cache, cache2):
            for i, val in iteritems({1:None, 2:4, 3:2}):
                self.assertEqual(c.field_for('rating', i), val)
            for i, val in iteritems({1:None, 2:4, 3:8}):
                self.assertEqual(c.field_for('#rating', i), val)
        del cache2

        # Series
        self.assertFalse(cache.set_field('series',
                {1:'a series one', 2:'a series one'}, allow_case_change=False))
        self.assertEqual(cache.set_field('series', {3:'Series [3]'}), {3})
        self.assertEqual(cache.set_field('#series', {1:'Series', 3:'Series'}),
                                         {1, 3})
        self.assertEqual(cache.set_field('#series', {2:'Series [0]'}), {2})
        cache2 = self.init_cache(cl)
        for c in (cache, cache2):
            for i, val in iteritems({1:'A Series One', 2:'A Series One', 3:'Series'}):
                self.assertEqual(c.field_for('series', i), val)
            cs_indices = {1:c.field_for('#series_index', 1), 3:c.field_for('#series_index', 3)}
            for i in (1, 2, 3):
                self.assertEqual(c.field_for('#series', i), 'Series')
            for i, val in iteritems({1:2, 2:1, 3:3}):
                self.assertEqual(c.field_for('series_index', i), val)
            for i, val in iteritems({1:cs_indices[1], 2:0, 3:cs_indices[3]}):
                self.assertEqual(c.field_for('#series_index', i), val)
        del cache2

    # }}}

    def test_many_many_basic(self):  # {{{
        'Test the different code paths for writing to a many-many field'
        cl = self.cloned_library
        cache = self.init_cache(cl)
        ae, af, sf = self.assertEqual, self.assertFalse, cache.set_field

        # Tags
        ae(sf('#tags', {1:cache.field_for('tags', 1), 2:cache.field_for('tags', 2)}),
            {1, 2})
        for name in ('tags', '#tags'):
            f = cache.fields[name]
            af(sf(name, {1:('News', 'tag one')}, allow_case_change=False))
            ae(sf(name, {1:'tag one, News'}), {1, 2})
            ae(sf(name, {3:('tag two', 'sep,sep2')}), {2, 3})
            ae(len(f.table.id_map), 4)
            ae(sf(name, {1:None}), {1})
            cache2 = self.init_cache(cl)
            for c in (cache, cache2):
                ae(c.field_for(name, 3), ('tag two', 'sep;sep2'))
                ae(len(c.fields[name].table.id_map), 3)
                ae(len(c.fields[name].table.id_map), 3)
                ae(c.field_for(name, 1), ())
                ae(c.field_for(name, 2), ('tag two', 'tag one'))
            del cache2

        # Authors
        ae(sf('#authors', {k:cache.field_for('authors', k) for k in (1,2,3)}),
           {1,2,3})

        for name in ('authors', '#authors'):
            f = cache.fields[name]
            ae(len(f.table.id_map), 3)
            af(cache.set_field(name, {3:'Unknown'}))
            ae(cache.set_field(name, {3:'Kovid Goyal & Divok Layog'}), {3})
            ae(cache.set_field(name, {1:'', 2:'An, Author'}), {1,2})
            cache2 = self.init_cache(cl)
            for c in (cache, cache2):
                ae(len(c.fields[name].table.id_map), 4 if name =='authors' else 3)
                ae(c.field_for(name, 3), ('Kovid Goyal', 'Divok Layog'))
                ae(c.field_for(name, 2), ('An, Author',))
                ae(c.field_for(name, 1), (_('Unknown'),) if name=='authors' else ())
                if name == 'authors':
                    ae(c.field_for('author_sort', 1), author_to_author_sort(_('Unknown')))
                    ae(c.field_for('author_sort', 2), author_to_author_sort('An, Author'))
                    ae(c.field_for('author_sort', 3), author_to_author_sort('Kovid Goyal') + ' & ' + author_to_author_sort('Divok Layog'))
            del cache2
        ae(cache.set_field('authors', {1:'KoviD GoyaL'}), {1, 3})
        ae(cache.field_for('author_sort', 1), 'GoyaL, KoviD')
        ae(cache.field_for('author_sort', 3), 'GoyaL, KoviD & Layog, Divok')

        # Languages
        f = cache.fields['languages']
        ae(f.table.id_map, {1: 'eng', 2: 'deu'})
        ae(sf('languages', {1:''}), {1})
        ae(cache.field_for('languages', 1), ())
        ae(sf('languages', {2:('und',)}), {2})
        af(f.table.id_map)
        ae(sf('languages', {1:'eng,fra,deu', 2:'es,Dutch', 3:'English'}), {1, 2, 3})
        ae(cache.field_for('languages', 1), ('eng', 'fra', 'deu'))
        ae(cache.field_for('languages', 2), ('spa', 'nld'))
        ae(cache.field_for('languages', 3), ('eng',))
        ae(sf('languages', {3:None}), {3})
        ae(cache.field_for('languages', 3), ())
        ae(sf('languages', {1:'deu,fra,eng'}), {1}, 'Changing order failed')
        ae(sf('languages', {2:'deu,eng,eng'}), {2})
        cache2 = self.init_cache(cl)
        for c in (cache, cache2):
            ae(cache.field_for('languages', 1), ('deu', 'fra', 'eng'))
            ae(cache.field_for('languages', 2), ('deu', 'eng'))
        del cache2

        # Identifiers
        f = cache.fields['identifiers']
        ae(sf('identifiers', {3: 'one:1,two:2'}), {3})
        ae(sf('identifiers', {2:None}), {2})
        ae(sf('identifiers', {1: {'test':'1', 'two':'2'}}), {1})
        cache2 = self.init_cache(cl)
        for c in (cache, cache2):
            ae(c.field_for('identifiers', 3), {'one':'1', 'two':'2'})
            ae(c.field_for('identifiers', 2), {})
            ae(c.field_for('identifiers', 1), {'test':'1', 'two':'2'})
        del cache2

        # Test setting of title sort
        ae(sf('title', {1:'The Moose', 2:'Cat'}), {1, 2})
        cache2 = self.init_cache(cl)
        for c in (cache, cache2):
            ae(c.field_for('sort', 1), title_sort('The Moose'))
            ae(c.field_for('sort', 2), title_sort('Cat'))

        # Test setting with the same value repeated
        ae(sf('tags', {3: ('a', 'b', 'a')}), {3})
        ae(sf('tags', {3: ('x', 'X')}), {3}, 'Failed when setting tag twice with different cases')
        ae(('x',), cache.field_for('tags', 3))

        # Test setting of authors with | in their names (for legacy database
        # format compatibility | is replaced by ,)
        ae(sf('authors', {3: ('Some| Author',)}), {3})
        ae(('Some, Author',), cache.field_for('authors', 3))

    # }}}

    def test_dirtied(self):  # {{{
        'Test the setting of the dirtied flag and the last_modified column'
        cl = self.cloned_library
        cache = self.init_cache(cl)
        ae, af, sf = self.assertEqual, self.assertFalse, cache.set_field
        # First empty dirtied
        cache.dump_metadata()
        af(cache.dirtied_cache)
        af(self.init_cache(cl).dirtied_cache)

        prev = cache.field_for('last_modified', 3)
        from datetime import timedelta

        import calibre.db.cache as c
        utime = prev+timedelta(days=1)
        onowf = c.nowf
        c.nowf = lambda: utime
        try:
            ae(sf('title', {3:'xxx'}), {3})
            self.assertTrue(3 in cache.dirtied_cache)
            ae(cache.field_for('last_modified', 3), utime)
            cache.dump_metadata()
            raw = cache.read_backup(3)
            from calibre.ebooks.metadata.opf2 import OPF
            opf = OPF(BytesIO(raw))
            ae(opf.title, 'xxx')
        finally:
            c.nowf = onowf
    # }}}

    def test_backup(self):  # {{{
        'Test the automatic backup of changed metadata'
        cl = self.cloned_library
        cache = self.init_cache(cl)
        ae, af, sf = self.assertEqual, self.assertFalse, cache.set_field
        # First empty dirtied
        cache.dump_metadata()
        af(cache.dirtied_cache)
        from calibre.db.backup import MetadataBackup
        interval = 0.01
        mb = MetadataBackup(cache, interval=interval, scheduling_interval=0)
        mb.start()
        try:
            ae(sf('title', {1:'title1', 2:'title2', 3:'title3'}), {1,2,3})
            ae(sf('authors', {1:'author1 & author2', 2:'author1 & author2', 3:'author1 & author2'}), {1,2,3})
            ae(sf('tags', {1:'tag1', 2:'tag1,tag2', 3:'XXX'}), {1,2,3})
            ae(cache.set_link_map('authors', {'author1': 'link1'}), {1,2,3})
            ae(cache.set_link_map('tags', {'XXX': 'YYY', 'tag2': 'link2'}), {2,3})
            count = 6
            while cache.dirty_queue_length() and count > 0:
                mb.join(2)
                count -= 1
            af(cache.dirty_queue_length())
        finally:
            mb.stop()
        mb.join(2)
        af(mb.is_alive())
        from calibre.ebooks.metadata.opf2 import OPF
        book_ids = (1,2,3)

        def read_all_formats():
            fbefore = {}
            for book_id in book_ids:
                ff = fbefore[book_id] = {}
                for fmt in cache.formats(book_id):
                    ff[fmt] = cache.format(book_id, fmt)
            return fbefore

        def read_all_extra_files(book_id=1):
            ans = {}
            bp = cache.field_for('path', book_id)
            for (relpath, fobj, stat_result) in cache.backend.iter_extra_files(book_id, bp, cache.fields['formats']):
                ans[relpath] = fobj.read()
            return ans

        for book_id in book_ids:
            raw = cache.read_backup(book_id)
            opf = OPF(BytesIO(raw))
            ae(opf.title, f'title{book_id}')
            ae(opf.authors, ['author1', 'author2'])
        tested_fields = 'title authors tags'.split()
        before = {f:cache.all_field_for(f, book_ids) for f in tested_fields}
        lbefore = tuple(cache.get_all_link_maps_for_book(i) for i in book_ids)
        fbefore = read_all_formats()
        bookdir = os.path.dirname(cache.format_abspath(1, '__COVER_INTERNAL__'))
        with open(os.path.join(bookdir, 'exf'), 'w') as f:
            f.write('exf')
        os.mkdir(os.path.join(bookdir, 'sub'))
        with open(os.path.join(bookdir, 'sub', 'recurse'), 'w') as f:
            f.write('recurse')
        ebefore = read_all_extra_files()
        authors = sorted(cache.all_field_ids('authors'))
        h1 = cache.add_notes_resource(b'resource1', 'r1.jpg')
        h2 = cache.add_notes_resource(b'resource2', 'r2.jpg')
        doc = f'simple notes for an author <img src="{RESOURCE_URL_SCHEME}://{h1.replace(":", "/",1)}"> '
        cache.set_notes_for('authors', authors[0], doc, resource_hashes=(h1,))
        doc += f'2 <img src="{RESOURCE_URL_SCHEME}://{h2.replace(":", "/",1)}">'
        cache.set_notes_for('authors', authors[1], doc, resource_hashes=(h1,h2))
        notes_before = {cache.get_item_name('authors', aid): cache.export_note('authors', aid) for aid in authors}
        cache.close()
        from calibre.db.restore import Restore
        restorer = Restore(cl)
        restorer.start()
        restorer.join(60)
        af(restorer.is_alive())
        cache = self.init_cache(cl)
        ae(before, {f:cache.all_field_for(f, book_ids) for f in tested_fields})
        ae(lbefore, tuple(cache.get_all_link_maps_for_book(i) for i in book_ids))
        ae(fbefore, read_all_formats())
        ae(ebefore, read_all_extra_files())
        authors = sorted(cache.all_field_ids('authors'))
        notes_after = {cache.get_item_name('authors', aid): cache.export_note('authors', aid) for aid in authors}
        ae(notes_before, notes_after)
    # }}}

    def test_set_cover(self):  # {{{
        ' Test setting of cover '
        cache = self.init_cache()
        ae = self.assertEqual

        # Test removing a cover
        ae(cache.field_for('cover', 1), 1)
        ae(cache.set_cover({1:None}), {1})
        ae(cache.field_for('cover', 1), 0)
        img = IMG

        # Test setting a cover
        ae(cache.set_cover({bid:img for bid in (1, 2, 3)}), {1, 2, 3})
        old = self.init_old()
        for book_id in (1, 2, 3):
            ae(cache.cover(book_id), img, f'Cover was not set correctly for book {book_id}')
            ae(cache.field_for('cover', book_id), 1)
            ae(old.cover(book_id, index_is_id=True), img, f'Cover was not set correctly for book {book_id}')
            self.assertTrue(old.has_cover(book_id))
        old.close()
        old.break_cycles()
        del old
    # }}}

    def test_set_metadata(self):  # {{{
        ' Test setting of metadata '
        ae = self.assertEqual
        cache = self.init_cache(self.cloned_library)

        # Check that changing title/author updates the path
        mi = cache.get_metadata(1)
        old_path = cache.field_for('path', 1)
        old_title, old_author = mi.title, mi.authors[0]
        ae(old_path, f'{old_author}/{old_title} (1)')
        mi.title, mi.authors = 'New Title', ['New Author']
        cache.set_metadata(1, mi)
        ae(cache.field_for('path', 1), f'{mi.authors[0]}/{mi.title} (1)')
        p = cache.format_abspath(1, 'FMT1')
        self.assertTrue(mi.authors[0] in p and mi.title in p)

        # Compare old and new set_metadata()
        db = self.init_old(self.cloned_library)
        mi = db.get_metadata(1, index_is_id=True, get_cover=True, cover_as_data=True)
        mi2 = db.get_metadata(3, index_is_id=True, get_cover=True, cover_as_data=True)
        db.set_metadata(2, mi)
        db.set_metadata(1, mi2, force_changes=True)
        oldmi = db.get_metadata(2, index_is_id=True, get_cover=True, cover_as_data=True)
        oldmi2 = db.get_metadata(1, index_is_id=True, get_cover=True, cover_as_data=True)
        db.close()
        del db
        cache = self.init_cache(self.cloned_library)
        cache.set_metadata(2, mi)
        nmi = cache.get_metadata(2, get_cover=True, cover_as_data=True)
        ae(oldmi.cover_data, nmi.cover_data)
        self.compare_metadata(nmi, oldmi, exclude={'last_modified', 'format_metadata', 'formats'})
        cache.set_metadata(1, mi2, force_changes=True)
        nmi2 = cache.get_metadata(1, get_cover=True, cover_as_data=True)
        self.compare_metadata(nmi2, oldmi2, exclude={'last_modified', 'format_metadata', 'formats'})

        cache = self.init_cache(self.cloned_library)
        mi = cache.get_metadata(1)
        otags = mi.tags
        mi.tags = [x.upper() for x in mi.tags]
        cache.set_metadata(3, mi)
        self.assertEqual(set(otags), set(cache.field_for('tags', 3)), 'case changes should not be allowed in set_metadata')

        # test that setting authors without author sort results in an
        # auto-generated authors sort
        mi = Metadata('empty', ['a1', 'a2'])
        cache.set_metadata(1, mi)
        self.assertEqual(cache.get_item_ids('authors', ('a1', 'a2')), cache.get_item_ids('authors', ('a1', 'a2'), case_sensitive=True))
        self.assertEqual(
            set(cache.get_item_ids('authors', ('A1', 'a2')).values()),
            set(cache.get_item_ids('authors', ('a1', 'a2'), case_sensitive=True).values()))
        self.assertEqual('a1 & a2', cache.field_for('author_sort', 1))
        cache.set_sort_for_authors({cache.get_item_id('authors', 'a1', case_sensitive=True): 'xy'})
        self.assertEqual('xy & a2', cache.field_for('author_sort', 1))
        mi = Metadata('empty', ['a1'])
        cache.set_metadata(1, mi)
        self.assertEqual('xy', cache.field_for('author_sort', 1))

    # }}}

    def test_conversion_options(self):  # {{{
        ' Test saving of conversion options '
        cache = self.init_cache()
        all_ids = cache.all_book_ids()
        self.assertFalse(cache.has_conversion_options(all_ids))
        self.assertIsNone(cache.conversion_options(1))
        op1, op2 = b"{'xx':'yy'}", b"{'yy':'zz'}"
        cache.set_conversion_options({1:op1, 2:op2})
        self.assertTrue(cache.has_conversion_options(all_ids))
        self.assertEqual(cache.conversion_options(1), op1)
        self.assertEqual(cache.conversion_options(2), op2)
        cache.set_conversion_options({1:op2})
        self.assertEqual(cache.conversion_options(1), op2)
        cache.delete_conversion_options(all_ids)
        self.assertFalse(cache.has_conversion_options(all_ids))
    # }}}

    def test_remove_items(self):  # {{{
        ' Test removal of many-(many,one) items '
        cache = self.init_cache()
        tmap = cache.get_id_map('tags')
        self.assertEqual(cache.remove_items('tags', tmap), {1, 2})
        tmap = cache.get_id_map('#tags')
        t = {v:k for k, v in iteritems(tmap)}['My Tag Two']
        self.assertEqual(cache.remove_items('#tags', (t,)), {1, 2})

        smap = cache.get_id_map('series')
        self.assertEqual(cache.remove_items('series', smap), {1, 2})
        smap = cache.get_id_map('#series')
        s = {v:k for k, v in iteritems(smap)}['My Series Two']
        self.assertEqual(cache.remove_items('#series', (s,)), {1})

        for c in (cache, self.init_cache()):
            self.assertFalse(c.get_id_map('tags'))
            self.assertFalse(c.all_field_names('tags'))
            for bid in c.all_book_ids():
                self.assertFalse(c.field_for('tags', bid))

            self.assertEqual(len(c.get_id_map('#tags')), 1)
            self.assertEqual(c.all_field_names('#tags'), {'My Tag One'})
            for bid in c.all_book_ids():
                self.assertIn(c.field_for('#tags', bid), ((), ('My Tag One',)))

            for bid in (1, 2):
                self.assertEqual(c.field_for('series_index', bid), 1.0)
            self.assertFalse(c.get_id_map('series'))
            self.assertFalse(c.all_field_names('series'))
            for bid in c.all_book_ids():
                self.assertFalse(c.field_for('series', bid))

            self.assertEqual(c.field_for('series_index', 1), 1.0)
            self.assertEqual(c.all_field_names('#series'), {'My Series One'})
            for bid in c.all_book_ids():
                self.assertIn(c.field_for('#series', bid), (None, 'My Series One'))

        # Now test with restriction
        cache = self.init_cache()
        cache.set_field('tags', {1:'a,b,c', 2:'b,a', 3:'x,y,z'})
        cache.set_field('series', {1:'a', 2:'a', 3:'b'})
        cache.set_field('series_index', {1:8, 2:9, 3:3})
        tmap, smap = cache.get_id_map('tags'), cache.get_id_map('series')
        self.assertEqual(cache.remove_items('tags', tmap, restrict_to_book_ids=()), set())
        self.assertEqual(cache.remove_items('tags', tmap, restrict_to_book_ids={1}), {1})
        self.assertEqual(cache.remove_items('series', smap, restrict_to_book_ids=()), set())
        self.assertEqual(cache.remove_items('series', smap, restrict_to_book_ids=(1,)), {1})
        c2 = self.init_cache()
        for c in (cache, c2):
            self.assertEqual(c.field_for('tags', 1), ())
            self.assertEqual(c.field_for('tags', 2), ('b', 'a'))
            self.assertNotIn('c', set(itervalues(c.get_id_map('tags'))))
            self.assertEqual(c.field_for('series', 1), None)
            self.assertEqual(c.field_for('series', 2), 'a')
            self.assertEqual(c.field_for('series_index', 1), 1.0)
            self.assertEqual(c.field_for('series_index', 2), 9)

    # }}}

    def test_rename_items(self):  # {{{
        ' Test renaming of many-(many,one) items '
        # Test renaming authors removes folders with junk in them
        cl = self.cloned_library
        cache = self.init_cache(cl)
        fmtpath = cache.format_abspath(1, 'FMT1')
        bookpath = os.path.dirname(fmtpath)
        authorpath = os.path.dirname(bookpath)
        self.assertTrue(os.path.exists(authorpath))
        author = cache.field_for('authors', 1)[0]
        os.mkdir(os.path.join(authorpath, '.DS_Store'))
        open(os.path.join(authorpath, 'Thumbs.db'), 'wb').close()
        amap = {v:k for k, v in cache.get_id_map('authors').items()}
        cache.rename_items('authors', {amap[author]: 'renamed'})
        try:
            items = os.listdir(authorpath)
        except FileNotFoundError:
            items = []
        self.assertFalse(items, 'Items in author folder: ' + ' '.join(items))

        cl = self.cloned_library
        cache = self.init_cache(cl)
        # Check that renaming authors updates author sort and path
        a = {v:k for k, v in iteritems(cache.get_id_map('authors'))}['Unknown']
        self.assertEqual(cache.rename_items('authors', {a:'New Author'})[0], {3})
        a = {v:k for k, v in iteritems(cache.get_id_map('authors'))}['Author One']
        self.assertEqual(cache.rename_items('authors', {a:'Author Two'})[0], {1, 2})
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('authors'), {'New Author', 'Author Two'})
            self.assertEqual(c.field_for('author_sort', 3), 'Author, New')
            self.assertIn('New Author/', c.field_for('path', 3))
            self.assertEqual(c.field_for('authors', 1), ('Author Two',))
            self.assertEqual(c.field_for('author_sort', 1), 'Two, Author')

        t = {v:k for k, v in iteritems(cache.get_id_map('tags'))}['Tag One']
        # Test case change
        self.assertEqual(cache.rename_items('tags', {t:'tag one'}), ({1, 2}, {t:t}))
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('tags'), {'tag one', 'Tag Two', 'News'})
            self.assertEqual(set(c.field_for('tags', 1)), {'tag one', 'News'})
            self.assertEqual(set(c.field_for('tags', 2)), {'tag one', 'Tag Two'})
        # Test new name
        self.assertEqual(cache.rename_items('tags', {t:'t1'})[0], {1,2})
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('tags'), {'t1', 'Tag Two', 'News'})
            self.assertEqual(set(c.field_for('tags', 1)), {'t1', 'News'})
            self.assertEqual(set(c.field_for('tags', 2)), {'t1', 'Tag Two'})
        # Test rename to existing
        self.assertEqual(cache.rename_items('tags', {t:'Tag Two'})[0], {1,2})
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('tags'), {'Tag Two', 'News'})
            self.assertEqual(set(c.field_for('tags', 1)), {'Tag Two', 'News'})
            self.assertEqual(set(c.field_for('tags', 2)), {'Tag Two'})
        # Test on a custom column
        t = {v:k for k, v in iteritems(cache.get_id_map('#tags'))}['My Tag One']
        self.assertEqual(cache.rename_items('#tags', {t:'My Tag Two'})[0], {2})
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('#tags'), {'My Tag Two'})
            self.assertEqual(set(c.field_for('#tags', 2)), {'My Tag Two'})

        # Test a Many-one field
        s = {v:k for k, v in iteritems(cache.get_id_map('series'))}['A Series One']
        # Test case change
        self.assertEqual(cache.rename_items('series', {s:'a series one'}), ({1, 2}, {s:s}))
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('series'), {'a series one'})
            self.assertEqual(c.field_for('series', 1), 'a series one')
            self.assertEqual(c.field_for('series_index', 1), 2.0)

        # Test new name
        self.assertEqual(cache.rename_items('series', {s:'series'})[0], {1, 2})
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('series'), {'series'})
            self.assertEqual(c.field_for('series', 1), 'series')
            self.assertEqual(c.field_for('series', 2), 'series')
            self.assertEqual(c.field_for('series_index', 1), 2.0)

        s = {v:k for k, v in iteritems(cache.get_id_map('#series'))}['My Series One']
        # Test custom column with rename to existing
        self.assertEqual(cache.rename_items('#series', {s:'My Series Two'})[0], {2})
        for c in (cache, self.init_cache(cl)):
            self.assertEqual(c.all_field_names('#series'), {'My Series Two'})
            self.assertEqual(c.field_for('#series', 2), 'My Series Two')
            self.assertEqual(c.field_for('#series_index', 1), 3.0)
            self.assertEqual(c.field_for('#series_index', 2), 4.0)

        # Test renaming many-many items to multiple items
        cache = self.init_cache(self.cloned_library)
        t = {v:k for k, v in iteritems(cache.get_id_map('tags'))}['Tag One']
        affected_books, id_map = cache.rename_items('tags', {t:'Something, Else, Entirely'})
        self.assertEqual({1, 2}, affected_books)
        tmap = cache.get_id_map('tags')
        self.assertEqual('Something', tmap[id_map[t]])
        self.assertEqual(1, len(id_map))
        f1, f2 = cache.field_for('tags', 1), cache.field_for('tags', 2)
        for f in (f1, f2):
            for t in 'Something,Else,Entirely'.split(','):
                self.assertIn(t, f)
            self.assertNotIn('Tag One', f)

        # Test with restriction
        cache = self.init_cache()
        cache.set_field('tags', {1:'a,b,c', 2:'x,y,z', 3:'a,x,z'})
        tmap = {v:k for k, v in iteritems(cache.get_id_map('tags'))}
        self.assertEqual(cache.rename_items('tags', {tmap['a']:'r'}, restrict_to_book_ids=()), (set(), {}))
        self.assertEqual(cache.rename_items('tags', {tmap['a']:'r', tmap['b']:'q'}, restrict_to_book_ids=(1,))[0], {1})
        self.assertEqual(cache.rename_items('tags', {tmap['x']:'X'}, restrict_to_book_ids=(2,))[0], {2})
        c2 = self.init_cache()
        for c in (cache, c2):
            self.assertEqual(c.field_for('tags', 1), ('r', 'q', 'c'))
            self.assertEqual(c.field_for('tags', 2), ('X', 'y', 'z'))
            self.assertEqual(c.field_for('tags', 3), ('a', 'X', 'z'))

    # }}}

    def test_composite_cache(self):  # {{{
        ' Test that the composite field cache is properly invalidated on writes '
        cache = self.init_cache()
        cache.create_custom_column('tc', 'TC', 'composite', False, display={
            'composite_template':'{title} {author_sort} {title_sort} {formats} {tags} {series} {series_index}'})
        cache = self.init_cache()

        def test_invalidate():
            c = self.init_cache()
            for bid in cache.all_book_ids():
                self.assertEqual(cache.field_for('#tc', bid), c.field_for('#tc', bid))

        cache.set_field('title', {1:'xx', 3:'yy'})
        test_invalidate()
        cache.set_field('series_index', {1:9, 3:11})
        test_invalidate()
        cache.rename_items('tags', {cache.get_item_id('tags', 'Tag One'):'xxx', cache.get_item_id('tags', 'News'):'news'})
        test_invalidate()
        cache.remove_items('tags', (cache.get_item_id('tags', 'news'),))
        test_invalidate()
        cache.set_sort_for_authors({cache.get_item_id('authors', 'Author One'):'meow'})
        test_invalidate()
        cache.remove_formats({1:{'FMT1'}})
        test_invalidate()
        cache.add_format(1, 'ADD', BytesIO(b'xxxx'))
        test_invalidate()
    # }}}

    def test_dump_and_restore(self):  # {{{
        ' Test roundtripping the db through SQL '
        import warnings
        with warnings.catch_warnings():
            # on python 3.10 apsw raises a deprecation warning which causes this test to fail on CI
            warnings.simplefilter('ignore', DeprecationWarning)
            cache = self.init_cache()
            uv = int(cache.backend.user_version)
            all_ids = cache.all_book_ids()
            cache.dump_and_restore()
            self.assertEqual(cache.set_field('title', {1:'nt'}), {1}, 'database connection broken')
            cache = self.init_cache()
            self.assertEqual(cache.all_book_ids(), all_ids, 'dump and restore broke database')
            self.assertEqual(int(cache.backend.user_version), uv)
    # }}}

    def test_set_author_data(self):  # {{{
        cache = self.init_cache()
        adata = cache.author_data()
        ldata = {aid:str(aid) for aid in adata}
        self.assertEqual({1,2,3}, cache.set_link_for_authors(ldata))
        for c in (cache, self.init_cache()):
            self.assertEqual(ldata, {aid:d['link'] for aid, d in iteritems(c.author_data())})
        self.assertEqual({3}, cache.set_link_for_authors({aid:'xxx' if aid == max(adata) else str(aid) for aid in adata}),
                         'Setting the author link to the same value as before, incorrectly marked some books as dirty')
        sdata = {aid:f'{aid}, changed' for aid in adata}
        self.assertEqual({1,2,3}, cache.set_sort_for_authors(sdata))
        for bid in (1, 2, 3):
            self.assertIn(', changed', cache.field_for('author_sort', bid))
        sdata = {aid:f'{aid*2 if aid == max(adata) else aid}, changed' for aid in adata}
        self.assertEqual({3}, cache.set_sort_for_authors(sdata),
                         'Setting the author sort to the same value as before, incorrectly marked some books as dirty')
    # }}}

    def test_fix_case_duplicates(self):  # {{{
        ' Test fixing of databases that have items in is_many fields that differ only by case '
        ae = self.assertEqual
        cache = self.init_cache()
        conn = cache.backend.conn
        conn.execute('INSERT INTO publishers (name) VALUES ("mūs")')
        lid = conn.last_insert_rowid()
        conn.execute('INSERT INTO publishers (name) VALUES ("MŪS")')
        uid = conn.last_insert_rowid()
        conn.execute('DELETE FROM books_publishers_link')
        conn.execute(f'INSERT INTO books_publishers_link (book,publisher) VALUES (1, {lid})')
        conn.execute(f'INSERT INTO books_publishers_link (book,publisher) VALUES (2, {uid})')
        conn.execute(f'INSERT INTO books_publishers_link (book,publisher) VALUES (3, {uid})')
        cache.reload_from_db()
        t = cache.fields['publisher'].table
        for x in (lid, uid):
            self.assertIn(x, t.id_map)
            self.assertIn(x, t.col_book_map)
        ae(t.book_col_map[1], lid)
        ae(t.book_col_map[2], uid)
        t.fix_case_duplicates(cache.backend)
        for c in (cache, self.init_cache()):
            t = c.fields['publisher'].table
            self.assertNotIn(uid, t.id_map)
            self.assertNotIn(uid, t.col_book_map)
            for bid in (1, 2, 3):
                ae(c.field_for('publisher', bid), 'mūs')
            c.close()

        cache = self.init_cache()
        conn = cache.backend.conn
        conn.execute('INSERT INTO tags (name) VALUES ("mūūs")')
        lid = conn.last_insert_rowid()
        conn.execute('INSERT INTO tags (name) VALUES ("MŪŪS")')
        uid = conn.last_insert_rowid()
        conn.execute('INSERT INTO tags (name) VALUES ("mūŪS")')
        mid = conn.last_insert_rowid()
        conn.execute('INSERT INTO tags (name) VALUES ("t")')
        norm = conn.last_insert_rowid()
        conn.execute('DELETE FROM books_tags_link')
        for book_id, vals in iteritems({1:(lid, uid), 2:(uid, mid), 3:(lid, norm)}):
            conn.executemany('INSERT INTO books_tags_link (book,tag) VALUES (?,?)',
                             tuple((book_id, x) for x in vals))
        cache.reload_from_db()
        t = cache.fields['tags'].table
        for x in (lid, uid, mid):
            self.assertIn(x, t.id_map)
            self.assertIn(x, t.col_book_map)
        t.fix_case_duplicates(cache.backend)
        for c in (cache, self.init_cache()):
            t = c.fields['tags'].table
            for x in (uid, mid):
                self.assertNotIn(x, t.id_map)
                self.assertNotIn(x, t.col_book_map)
            ae(c.field_for('tags', 1), (t.id_map[lid],))
            ae(c.field_for('tags', 2), (t.id_map[lid],), 'failed for book 2')
            ae(c.field_for('tags', 3), (t.id_map[lid], t.id_map[norm]))
    # }}}

    def test_preferences(self):  # {{{
        ' Test getting and setting of preferences, especially with mutable objects '
        cache = self.init_cache()
        changes = []
        cache.backend.conn.setupdatehook(lambda typ, dbname, tblname, rowid: changes.append(rowid))
        prefs = cache.backend.prefs
        prefs['test mutable'] = [1, 2, 3]
        self.assertEqual(len(changes), 1)
        a = prefs['test mutable']
        a.append(4)
        self.assertIn(4, prefs['test mutable'])
        prefs['test mutable'] = a
        self.assertEqual(len(changes), 2)
        prefs.load_from_db()
        self.assertIn(4, prefs['test mutable'])
        prefs['test mutable'] = {k:k for k in range(10)}
        self.assertEqual(len(changes), 3)
        prefs['test mutable'] = {k:k for k in reversed(range(10))}
        self.assertEqual(len(changes), 3, 'The database was written to despite there being no change in value')
    # }}}

    def test_annotations(self):  # {{{
        'Test handling of annotations'
        from calibre.utils.date import EPOCH, utcnow
        cl = self.cloned_library
        cache = self.init_cache(cl)
        # First empty dirtied
        cache.dump_metadata()
        self.assertFalse(cache.dirtied_cache)

        def a(**kw):
            ts = utcnow()
            kw['timestamp'] = utcnow().isoformat()
            return kw, (ts - EPOCH).total_seconds()

        annot_list = [
            a(type='bookmark', title='bookmark1 changed', seq=1),
            a(type='highlight', highlighted_text='text1', uuid='1', seq=2),
            a(type='highlight', highlighted_text='text2', uuid='2', seq=3, notes='notes2 some word changed again'),
        ]

        def map_as_list(amap):
            ans = []
            for items in amap.values():
                ans.extend(items)
            ans.sort(key=lambda x:x['seq'])
            return ans

        cache.set_annotations_for_book(1, 'moo', annot_list)
        amap = cache.annotations_map_for_book(1, 'moo')
        self.assertEqual(3, len(cache.all_annotations_for_book(1)))
        self.assertEqual([x[0] for x in annot_list], map_as_list(amap))
        self.assertFalse(cache.dirtied_cache)
        cache.check_dirtied_annotations()
        self.assertEqual(set(cache.dirtied_cache), {1})
        cache.dump_metadata()
        cache.check_dirtied_annotations()
        self.assertFalse(cache.dirtied_cache)

        # Test searching
        results = cache.search_annotations('"changed"')
        self.assertEqual([1, 3], [x['id'] for x in results])
        results = cache.search_annotations('"changed"', annotation_type='bookmark')
        self.assertEqual([1], [x['id'] for x in results])
        results = cache.search_annotations('"Changed"')  # changed and change stem differently in english and other euro languages
        self.assertEqual([1, 3], [x['id'] for x in results])
        results = cache.search_annotations('"SOMe"')
        self.assertEqual([3], [x['id'] for x in results])
        results = cache.search_annotations('"change"', use_stemming=False)
        self.assertFalse(results)
        results = cache.search_annotations('"bookmark1"', highlight_start='[', highlight_end=']')
        self.assertEqual(results[0]['text'], '[bookmark1] changed')
        results = cache.search_annotations('"word"', highlight_start='[', highlight_end=']', snippet_size=3)
        self.assertEqual(results[0]['text'], '…some [word] changed…')
        self.assertRaises(FTSQueryError, cache.search_annotations, 'AND OR')
        fts_l = [a(type='bookmark', title='路坎坷走来', seq=1),]
        cache.set_annotations_for_book(1, 'moo', fts_l)
        results = cache.search_annotations('路', highlight_start='[', highlight_end=']')
        self.assertEqual(results[0]['text'], '[路]坎坷走来')

        annot_list[0][0]['title'] = 'changed title'
        cache.set_annotations_for_book(1, 'moo', annot_list)
        amap = cache.annotations_map_for_book(1, 'moo')
        self.assertEqual([x[0] for x in annot_list], map_as_list(amap))

        del annot_list[1]
        cache.set_annotations_for_book(1, 'moo', annot_list)
        amap = cache.annotations_map_for_book(1, 'moo')
        self.assertEqual([x[0] for x in annot_list], map_as_list(amap))
        cache.check_dirtied_annotations()
        cache.dump_metadata()
        from calibre.ebooks.metadata.opf2 import OPF
        raw = cache.read_backup(1)
        opf = OPF(BytesIO(raw))
        cache.restore_annotations(1, list(opf.read_annotations()))
        amap = cache.annotations_map_for_book(1, 'moo')
        self.assertEqual([x[0] for x in annot_list], map_as_list(amap))
    # }}}

    def test_changed_events(self):  # {{{
        def ae(l, r):
            # We need to sleep a bit to allow events to happen on its thread
            import time
            st = time.monotonic()
            while time.monotonic() - st < 1:
                time.sleep(0.01)
                try:
                    self.assertEqual(l, r)
                    return
                except Exception:
                    pass
            self.assertEqual(l, r)

        cache = self.init_cache(self.cloned_library)
        ae(cache.all_book_ids(), {1, 2, 3})

        event_set = set()

        def event_func(t, library_id, *args):
            nonlocal event_set, ae
            event_set.update(args[0][1])
        cache.add_listener(event_func)

        # Test that setting metadata to itself doesn't generate any events
        for id_ in cache.all_book_ids():
            cache.set_metadata(id_, cache.get_metadata(id_))

        ae(event_set, set())

        # test setting a single field
        cache.set_field('tags', {1:'foo'})
        ae(event_set, {1})

        # test setting multiple books. Book 1 shouldn't get an event because it
        # isn't being changed
        event_set = set()
        cache.set_field('tags', {1:'foo', 2:'bar', 3:'mumble'})
        ae(event_set, {2, 3})

        # test setting a many-many field to empty
        event_set = set()
        cache.set_field('tags', {1:''})
        ae(event_set, {1,})
        event_set = set()
        cache.set_field('tags', {1:''})
        ae(event_set, set())

        # test setting title
        event_set = set()
        cache.set_field('title', {1:'Book 1'})
        ae(event_set, {1})
        ae(cache.field_for('title', 1), 'Book 1')

        # test setting series
        event_set = set()
        cache.set_field('series', {1:'GreatBooks [1]'})
        cache.set_field('series', {2:'GreatBooks [0]'})
        ae(event_set, {1,2})
        ae(cache.field_for('series', 1), 'GreatBooks')
        ae(cache.field_for('series_index', 1), 1.0)
        ae(cache.field_for('series', 2), 'GreatBooks')
        ae(cache.field_for('series_index', 2), 0.0)

        # now series_index
        event_set = set()
        cache.set_field('series_index', {1:2})
        ae(event_set, {1})
        ae(cache.field_for('series_index', 1), 2.0)
        event_set = set()
        cache.set_field('series_index', {1:2, 2:3.5})  # book 1 isn't changed
        ae(event_set, {2})
        ae(cache.field_for('series_index', 1), 2.0)
        ae(cache.field_for('series_index', 2), 3.5)

    def test_link_maps(self):
        cache = self.init_cache()

        # Add two tags
        cache.set_field('tags', {1:'foo'})
        self.assertEqual(('foo',), cache.field_for('tags', 1), 'Setting tag foo failed')
        cache.set_field('tags', {1:'foo, bar'})
        self.assertEqual(('foo', 'bar'), cache.field_for('tags', 1), 'Adding second tag failed')

        # Check adding a link
        links = cache.get_link_map('tags')
        self.assertDictEqual(links, {}, 'Initial tags link dict is not empty')
        links['foo'] = 'url'
        cache.set_link_map('tags', links)
        links2 = cache.get_link_map('tags')
        self.assertDictEqual(links2, links, 'tags link dict mismatch')

        # Check getting links for a book and that links are correct
        cache.set_field('publisher', {1:'random'})
        cache.set_link_map('publisher', {'random': 'url2'})
        links = cache.get_all_link_maps_for_book(1)
        self.assertSetEqual(set(links.keys()), {'tags', 'publisher'}, 'Wrong link keys')
        self.assertSetEqual(set(links['tags'].keys()), {'foo', }, 'Should be "foo"')
        self.assertSetEqual(set(links['publisher'].keys()), {'random', }, 'Should be "random"')
        self.assertEqual('url', links['tags']['foo'], 'link for tag foo is wrong')
        self.assertEqual('url2', links['publisher']['random'], 'link for publisher random is wrong')

        # Check that renaming a tag keeps the link and clears the link map cache for the book
        self.assertTrue(1 in cache.link_maps_cache, 'book not in link_map_cache')
        tag_id = cache.get_item_id('tags', 'foo')
        cache.rename_items('tags', {tag_id: 'foobar'})
        self.assertTrue(1 not in cache.link_maps_cache, 'book still in link_map_cache')
        links = cache.get_link_map('tags')
        self.assertTrue('foobar' in links, 'rename foo lost the link')
        self.assertEqual(links['foobar'], 'url', 'The link changed contents')
        links = cache.get_all_link_maps_for_book(1)
        self.assertTrue(1 in cache.link_maps_cache, 'book not put back into link_map_cache')
        self.assertDictEqual({'publisher': {'random': 'url2'}, 'tags': {'foobar': 'url'}},
                             links, 'book links incorrect after tag rename')

        # Check ProxyMetadata
        mi = cache.get_proxy_metadata(1)
        self.assertDictEqual({'publisher': {'random': 'url2'}, 'tags': {'foobar': 'url'}},
                             mi.link_maps, "ProxyMetadata didn't return the right link map")

        # Now test deleting the links.
        links = cache.get_link_map('tags')
        to_del = {l:'' for l in links.keys()}
        cache.set_link_map('tags', to_del)
        self.assertEqual({}, cache.get_link_map('tags'), 'links on tags were not deleted')
        links = cache.get_link_map('publisher')
        to_del = {l:'' for l in links.keys()}
        cache.set_link_map('publisher', to_del)
        self.assertEqual({}, cache.get_link_map('publisher'), 'links on publisher were not deleted')
        self.assertEqual({}, cache.get_all_link_maps_for_book(1), 'Not all links for book were deleted')
    # }}}
