#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2009, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import os
import re

from qt.core import QApplication, QPixmap

from calibre.ebooks.conversion.config import OPTIONS
from calibre.ebooks.metadata import MetaInformation, string_to_authors, title_sort
from calibre.ebooks.metadata.opf2 import metadata_to_opf
from calibre.gui2 import choose_images, error_dialog
from calibre.gui2.convert import Widget
from calibre.gui2.convert.metadata_ui import Ui_Form
from calibre.library.comments import comments_to_html
from calibre.ptempfile import PersistentTemporaryFile
from calibre.utils.config import tweaks
from calibre.utils.icu import sort_key


def create_opf_file(db, book_id, opf_file=None):
    mi = db.get_metadata(book_id, index_is_id=True)
    old_cover = mi.cover
    mi.cover = None
    mi.application_id = mi.uuid
    raw = metadata_to_opf(mi)
    mi.cover = old_cover
    if opf_file is None:
        opf_file = PersistentTemporaryFile('.opf')
    opf_file.write(raw)
    opf_file.close()
    return mi, opf_file


def create_cover_file(db, book_id):
    cover = db.cover(book_id, index_is_id=True)
    cf = None
    if cover:
        cf = PersistentTemporaryFile('.jpeg')
        cf.write(cover)
        cf.close()
    return cf


class MetadataWidget(Widget, Ui_Form):

    TITLE = _('Metadata')
    ICON  = 'dialog_information.png'
    HELP  = _('Set the metadata. The output file will contain as much of this '
            'metadata as possible.')
    COMMIT_NAME = 'metadata'

    def __init__(self, parent, get_option, get_help, db=None, book_id=None):
        Widget.__init__(self, parent, OPTIONS['pipe']['metadata'])
        self.db, self.book_id = db, book_id
        self.cover_changed = False
        self.cover_data = None
        if self.db is not None:
            self.initialize_metadata_options()
        self.initialize_options(get_option, get_help, db, book_id)
        self.cover_button.clicked.connect(self.select_cover)
        self.comment.hide_toolbars()
        self.cover.cover_changed.connect(self.change_cover)
        self.series.currentTextChanged.connect(self.series_changed)
        cuh = self.db.new_api.pref('categories_using_hierarchy', default=())
        if 'series' in cuh:
            self.series.set_hierarchy_separator('.')
        if 'tags' in cuh:
            self.tags.set_hierarchy_separator('.')
        self.cover.draw_border = False

    def change_cover(self, data):
        self.cover_changed = True
        self.cover_data = data

    def deduce_author_sort(self, *args):
        au = str(self.author.currentText())
        au = re.sub(r'\s+et al\.$', '', au)
        authors = string_to_authors(au)
        self.author_sort.setText(self.db.author_sort_from_authors(authors))
        self.author_sort.home(False)

    def initialize_metadata_options(self):
        self.initialize_combos()
        self.author.editTextChanged.connect(self.deduce_author_sort)

        mi = self.db.get_metadata(self.book_id, index_is_id=True)
        self.title.setText(mi.title), self.title.home(False)
        self.publisher.show_initial_value(mi.publisher if mi.publisher else '')
        self.publisher.home(False)
        self.author_sort.setText(mi.author_sort if mi.author_sort else '')
        self.author_sort.home(False)
        self.tags.setText(', '.join(mi.tags if mi.tags else []))
        self.tags.update_items_cache(self.db.new_api.all_field_names('tags'))
        self.tags.home(False)
        self.comment.html = comments_to_html(mi.comments) if mi.comments else ''
        self.series.show_initial_value(mi.series if mi.series else '')
        self.series.home(False)
        if mi.series_index is not None:
            try:
                self.series_index.setValue(mi.series_index)
            except Exception:
                self.series_index.setValue(1.0)

        cover = self.db.cover(self.book_id, index_is_id=True)
        if cover:
            pm = QPixmap()
            pm.loadFromData(cover)
            if not pm.isNull():
                pm.setDevicePixelRatio(self.devicePixelRatio())
                self.cover.setPixmap(pm)
                self.cover_data = cover
                self.set_cover_tooltip(pm)
        else:
            pm = QApplication.instance().cached_qpixmap('default_cover.png', device_pixel_ratio=self.devicePixelRatio())
            self.cover.setPixmap(pm)
            self.cover.setToolTip(_('This book has no cover'))
        for x in ('author', 'series', 'publisher'):
            x = getattr(self, x)
            x.lineEdit().deselect()
        self.series_changed()

    def series_changed(self):
        self.series_index.setEnabled(len(self.series.currentText().strip()) > 0)

    def set_cover_tooltip(self, pm):
        tt = _('Cover size: %(width)d x %(height)d pixels') % dict(
                width=pm.width(), height=pm.height())
        self.cover.setToolTip(tt)

    def initialize_combos(self):
        self.initalize_authors()
        self.initialize_series()
        self.initialize_publisher()

    def initalize_authors(self):
        all_authors = self.db.all_authors()
        all_authors.sort(key=lambda x: sort_key(x[1]))
        self.author.set_separator('&')
        self.author.set_space_before_sep(True)
        self.author.set_add_separator(tweaks['authors_completer_append_separator'])
        self.author.update_items_cache(self.db.new_api.all_field_names('authors'))

        au = self.db.authors(self.book_id, True)
        if not au:
            au = _('Unknown')
        au = ' & '.join([a.strip().replace('|', ',') for a in au.split(',')])
        self.author.show_initial_value(au)
        self.author.home(False)

    def initialize_series(self):
        self.series.set_separator(None)
        self.series.update_items_cache(self.db.new_api.all_field_names('series'))

    def initialize_publisher(self):
        self.publisher.set_separator(None)
        self.publisher.update_items_cache(self.db.new_api.all_field_names('publisher'))

    def get_title_and_authors(self):
        title = str(self.title.text()).strip()
        if not title:
            title = _('Unknown')
        authors = str(self.author.text()).strip()
        authors = string_to_authors(authors) if authors else [_('Unknown')]
        return title, authors

    def get_metadata(self):
        title, authors = self.get_title_and_authors()
        mi = MetaInformation(title, authors)
        publisher = str(self.publisher.text()).strip()
        if publisher:
            mi.publisher = publisher
        author_sort = str(self.author_sort.text()).strip()
        if author_sort:
            mi.author_sort = author_sort
        comments = self.comment.html
        if comments:
            mi.comments = comments
        mi.series_index = float(self.series_index.value())
        series = str(self.series.currentText()).strip()
        if series:
            mi.series = series
        tags = [t.strip() for t in str(self.tags.text()).strip().split(',')]
        if tags:
            mi.tags = tags

        return mi

    def select_cover(self):
        files = choose_images(self, 'change cover dialog',
                             _('Choose cover for ') + str(self.title.text()))
        if not files:
            return
        _file = files[0]
        if _file:
            _file = os.path.abspath(_file)
            if not os.access(_file, os.R_OK):
                d = error_dialog(self.parent(), _('Cannot read'),
                        _('You do not have permission to read the file: ') + _file)
                d.exec()
                return
            cover = None
            try:
                with open(_file, 'rb') as f:
                    cover = f.read()
            except OSError as e:
                d = error_dialog(self.parent(), _('Error reading file'),
                        _('<p>There was an error reading from file: <br /><b>') + _file + '</b></p><br />'+str(e))
                d.exec()
            if cover:
                pix = QPixmap()
                pix.loadFromData(cover)
                pix.setDevicePixelRatio(getattr(self, 'devicePixelRatioF', self.devicePixelRatio)())
                if pix.isNull():
                    d = error_dialog(self.parent(), _('Error reading file'),
                                      _file + _(' is not a valid picture'))
                    d.exec()
                else:
                    self.cover_path.setText(_file)
                    self.set_cover_tooltip(pix)
                    self.cover.setPixmap(pix)
                    self.cover_changed = True
                    self.cpixmap = pix
                    self.cover_data = cover

    def get_recommendations(self):
        return {
            'prefer_metadata_cover': bool(self.opt_prefer_metadata_cover.isChecked()),
        }

    def pre_commit_check(self):
        if self.db is None:
            return True
        db = self.db.new_api
        title, authors = self.get_title_and_authors()
        try:
            if title != db.field_for('title', self.book_id):
                db.set_field('title', {self.book_id:title})
                langs = db.field_for('languages', self.book_id)
                if langs:
                    db.set_field('sort', {self.book_id:title_sort(title, langs[0])})
            if list(authors) != list(db.field_for('authors', self.book_id)):
                db.set_field('authors', {self.book_id:authors})
            if self.cover_changed and self.cover_data is not None:
                self.db.set_cover(self.book_id, self.cover_data)
        except OSError as err:
            err.locking_violation_msg = _("Failed to change on disk location of this book's files.")
            raise
        publisher = self.publisher.text().strip()
        if publisher != db.field_for('publisher', self.book_id):
            db.set_field('publisher', {self.book_id:publisher})
        author_sort = self.author_sort.text().strip()
        if author_sort != db.field_for('author_sort', self.book_id):
            db.set_field('author_sort', {self.book_id:author_sort})
        tags = [t.strip() for t in self.tags.text().strip().split(',')]
        if tags != list(db.field_for('tags', self.book_id)):
            db.set_field('tags', {self.book_id:tags})
        series_index = float(self.series_index.value())
        series = self.series.currentText().strip()
        if series != db.field_for('series', self.book_id):
            db.set_field('series', {self.book_id:series})
        if series and series_index != db.field_for('series_index', self.book_id):
            db.set_field('series_index', {self.book_id:series_index})
        return True

    def commit(self, save_defaults=False):
        '''
        Settings are stored in two attributes: `opf_file` and `cover_file`.
        Both may be None. Also returns a recommendation dictionary.
        '''
        recs = self.commit_options(save_defaults)
        self.user_mi = self.get_metadata()
        self.cover_file = self.opf_file = None
        if self.db is not None:
            self.mi, self.opf_file = create_opf_file(self.db, self.book_id)
            cover = self.db.cover(self.book_id, index_is_id=True)
            if cover:
                cf = PersistentTemporaryFile('.jpeg')
                cf.write(cover)
                cf.close()
                self.cover_file = cf
        return recs
