#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2011, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import numbers
import textwrap

from qt.core import QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QLabel, QLineEdit, QListView, QSpinBox, Qt, QVBoxLayout, QWidget

from calibre.gui2.preferences.metadata_sources import FieldsModel as FM
from calibre.utils.icu import sort_key
from polyglot.builtins import iteritems


class FieldsModel(FM):  # {{{

    def __init__(self, plugin):
        FM.__init__(self)
        self.plugin = plugin
        self.exclude = frozenset(['title', 'authors']) | self.exclude
        self.prefs = self.plugin.prefs

    def initialize(self):
        fields = self.plugin.touched_fields
        self.beginResetModel()
        self.fields = []
        for x in fields:
            if not x.startswith('identifier:') and x not in self.exclude:
                self.fields.append(x)
        self.fields.sort(key=lambda x: self.descs.get(x, x))
        self.endResetModel()

    def state(self, field, defaults=False):
        src = self.prefs.defaults if defaults else self.prefs
        return (Qt.CheckState.Unchecked if field in src['ignore_fields']
                    else Qt.CheckState.Checked)

    def restore_defaults(self):
        self.beginResetModel()
        self.overrides = {f: self.state(f, True) for f in self.fields}
        self.endResetModel()

    def commit(self):
        ignored_fields = {x for x in self.prefs['ignore_fields'] if x not in
            self.overrides}
        changed = {k for k, v in iteritems(self.overrides) if v ==
            Qt.CheckState.Unchecked}
        self.prefs['ignore_fields'] = list(ignored_fields.union(changed))

# }}}


class FieldsList(QListView):

    def sizeHint(self):
        return self.minimumSizeHint()


class ConfigWidget(QWidget):

    def __init__(self, plugin):
        QWidget.__init__(self)
        self.plugin = plugin

        self.overl = l = QVBoxLayout(self)
        self.gb = QGroupBox(_('Metadata fields to download'), self)
        if plugin.config_help_message:
            self.pchm = QLabel(plugin.config_help_message)
            self.pchm.setWordWrap(True)
            self.pchm.setOpenExternalLinks(True)
            l.addWidget(self.pchm, 10)
        l.addWidget(self.gb)
        self.gb.l = g = QVBoxLayout(self.gb)
        g.setContentsMargins(0, 0, 0, 0)
        self.fields_view = v = FieldsList(self)
        g.addWidget(v)
        v.setFlow(QListView.Flow.LeftToRight)
        v.setWrapping(True)
        v.setResizeMode(QListView.ResizeMode.Adjust)
        self.fields_model = FieldsModel(self.plugin)
        self.fields_model.initialize()
        v.setModel(self.fields_model)
        self.memory = []
        self.widgets = []
        self.l = QGridLayout()
        self.l.setContentsMargins(0, 0, 0, 0)
        l.addLayout(self.l, 100)
        for opt in plugin.options:
            self.create_widgets(opt)

    def create_widgets(self, opt):
        val = self.plugin.prefs[opt.name]
        if opt.type == 'number':
            c = QSpinBox if isinstance(opt.default, numbers.Integral) else QDoubleSpinBox
            widget = c(self)
            widget.setRange(min(widget.minimum(), 20 * val), max(widget.maximum(), 20 * val))
            widget.setValue(val)
        elif opt.type == 'string':
            widget = QLineEdit(self)
            widget.setText(val if val else '')
        elif opt.type == 'bool':
            widget = QCheckBox(opt.label, self)
            widget.setChecked(bool(val))
        elif opt.type == 'choices':
            widget = QComboBox(self)
            items = list(iteritems(opt.choices))
            items.sort(key=lambda k_v: sort_key(k_v[1]))
            for key, label in items:
                widget.addItem(label, (key))
            idx = widget.findData(val)
            widget.setCurrentIndex(idx)
        widget.opt = opt
        widget.setToolTip(textwrap.fill(opt.desc))
        self.widgets.append(widget)
        r = self.l.rowCount()
        if opt.type == 'bool':
            self.l.addWidget(widget, r, 0, 1, self.l.columnCount())
        else:
            l = QLabel(opt.label)
            l.setToolTip(widget.toolTip())
            self.memory.append(l)
            l.setBuddy(widget)
            self.l.addWidget(l, r, 0, 1, 1)
            self.l.addWidget(widget, r, 1, 1, 1)

    def commit(self):
        self.fields_model.commit()
        for w in self.widgets:
            if isinstance(w, (QSpinBox, QDoubleSpinBox)):
                val = w.value()
            elif isinstance(w, QLineEdit):
                val = str(w.text())
            elif isinstance(w, QCheckBox):
                val = w.isChecked()
            elif isinstance(w, QComboBox):
                idx = w.currentIndex()
                val = str(w.itemData(idx) or '')
            self.plugin.prefs[w.opt.name] = val
