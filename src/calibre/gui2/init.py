#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2010, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import functools

from qt.core import (
    QAction,
    QApplication,
    QDialog,
    QEvent,
    QIcon,
    QLabel,
    QMenu,
    QPixmap,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QStyleOption,
    QStylePainter,
    Qt,
    QTabBar,
    QTimer,
    QToolButton,
    QUrl,
)

from calibre.constants import get_appname_for_display, get_version, ismacos
from calibre.customize.ui import find_plugin
from calibre.gui2 import config, error_dialog, gprefs, open_local_file, open_url
from calibre.gui2.book_details import BookDetails
from calibre.gui2.central import CentralContainer, LayoutButton
from calibre.gui2.layout_menu import LayoutMenu
from calibre.gui2.library.alternate_views import GridView
from calibre.gui2.library.views import BooksView, DeviceBooksView
from calibre.gui2.notify import get_notifier
from calibre.gui2.tag_browser.ui import TagBrowserWidget
from calibre.utils.config import prefs
from calibre.utils.icu import sort_key
from calibre.utils.localization import localize_website_link, ngettext

_keep_refs = []


def partial(*args, **kwargs):
    ans = functools.partial(*args, **kwargs)
    _keep_refs.append(ans)
    return ans


class LibraryViewMixin:  # {{{

    def __init__(self, *args, **kwargs):
        pass

    def init_library_view_mixin(self, db):
        self.library_view.files_dropped.connect(self.iactions['Add Books'].files_dropped, type=Qt.ConnectionType.QueuedConnection)
        self.library_view.books_dropped.connect(self.iactions['Edit Metadata'].books_dropped, type=Qt.ConnectionType.QueuedConnection)
        self.library_view.add_column_signal.connect(partial(self.iactions['Preferences'].do_config,
            initial_plugin=('Interface', 'Custom Columns'), close_after_initial=True),
                type=Qt.ConnectionType.QueuedConnection)
        for func, args in [
                             ('connect_to_search_box', (self.search,
                                 self.search_done)),
                             ('connect_to_book_display',
                                 (self.book_details.show_data,)),
                             ]:
            for view in (self.library_view, self.memory_view, self.card_a_view, self.card_b_view):
                getattr(view, func)(*args)

        self.memory_view.connect_dirtied_signal(self.upload_dirtied_booklists)
        self.memory_view.connect_upload_collections_signal(
                                    func=self.upload_collections, oncard=None)
        self.card_a_view.connect_dirtied_signal(self.upload_dirtied_booklists)
        self.card_a_view.connect_upload_collections_signal(
                                    func=self.upload_collections, oncard='carda')
        self.card_b_view.connect_dirtied_signal(self.upload_dirtied_booklists)
        self.card_b_view.connect_upload_collections_signal(
                                    func=self.upload_collections, oncard='cardb')
        self.book_on_device(None, reset=True)
        db.set_book_on_device_func(self.book_on_device)
        self.library_view.set_database(db)
        self.library_view.model().set_book_on_device_func(self.book_on_device)
        prefs['library_path'] = self.library_path

        for view in ('library', 'memory', 'card_a', 'card_b'):
            view = getattr(self, view+'_view')
            view.verticalHeader().sectionDoubleClicked.connect(self.iactions['View'].view_specific_book)

        self.library_view.model().set_highlight_only(config['highlight_search_matches'])

    def build_context_menus(self):
        from calibre.gui2.bars import populate_menu
        lm = QMenu(self)
        populate_menu(lm, gprefs['action-layout-context-menu'], self.iactions)
        dm = QMenu(self)
        populate_menu(dm, gprefs['action-layout-context-menu-device'], self.iactions)
        ec = self.iactions['Edit Collections'].qaction
        self.library_view.set_context_menu(lm, ec)
        sm = QMenu(self)
        populate_menu(sm, gprefs['action-layout-context-menu-split'], self.iactions)
        self.library_view.pin_view.set_context_menu(sm)
        for v in (self.memory_view, self.card_a_view, self.card_b_view):
            v.set_context_menu(dm, ec)

        if hasattr(self.cover_flow, 'set_context_menu'):
            cm = QMenu(self.cover_flow)
            populate_menu(cm,
                    gprefs['action-layout-context-menu-cover-browser'], self.iactions)
            self.cover_flow.set_context_menu(cm)

    def search_done(self, view, ok):
        if view is self.current_view():
            self.search.search_done(ok)
            self.set_number_of_books_shown()
            if ok:
                v = self.current_view()
                if hasattr(v, 'set_current_row'):
                    v.set_current_row(0)
                    if v is self.library_view and v.row_count() == 0:
                        self.book_details.reset_info()
# }}}


class UpdateLabel(QLabel):  # {{{

    def __init__(self, *args, **kwargs):
        QLabel.__init__(self, *args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def contextMenuEvent(self, e):
        pass
# }}}


class VersionLabel(QLabel):  # {{{

    def __init__(self, parent):
        QLabel.__init__(self, parent)
        self.mouse_over = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(_("See what's new in this calibre release"))

    def mouseReleaseEvent(self, ev):
        open_url(localize_website_link('https://calibre-ebook.com/whats-new'))
        ev.accept()
        return QLabel.mouseReleaseEvent(self, ev)

    def event(self, ev):
        m = None
        et = ev.type()
        if et == QEvent.Type.Enter:
            m = True
        elif et == QEvent.Type.Leave:
            m = False
        if m is not None and m != self.mouse_over:
            self.mouse_over = m
            self.update()
        return QLabel.event(self, ev)

    def paintEvent(self, ev):
        if self.mouse_over:
            p = QStylePainter(self)
            tool = QStyleOption()
            tool.initFrom(self)
            tool.rect = self.rect()
            tool.state = QStyle.StateFlag.State_Raised | QStyle.StateFlag.State_Active | QStyle.StateFlag.State_MouseOver
            p.drawPrimitive(QStyle.PrimitiveElement.PE_PanelButtonTool, tool)
            p.end()
        return QLabel.paintEvent(self, ev)
# }}}


class StatusBar(QStatusBar):  # {{{

    def __init__(self, parent=None):
        QStatusBar.__init__(self, parent)
        self.version = get_version()
        self.base_msg = f'{get_appname_for_display()} {self.version}'
        self.device_string = ''
        self.update_label = UpdateLabel('')
        self.total = self.current = self.selected = self.library_total = 0
        self.addPermanentWidget(self.update_label)
        self.update_label.setVisible(False)
        self.defmsg = VersionLabel(self)
        self.addWidget(self.defmsg)
        self.set_label()

    def initialize(self, systray=None):
        self.systray = systray
        self.notifier = get_notifier(systray)

    def device_connected(self, devname):
        self.device_string = _('Connected ') + devname
        self.set_label()

    def update_state(self, library_total, total, current, selected):
        self.library_total = library_total
        self.total, self.current, self.selected = total, current, selected
        self.set_label()

    def set_label(self):
        try:
            self._set_label()
        except Exception:
            import traceback
            traceback.print_exc()

    def _set_label(self):
        msg = self.base_msg
        if self.device_string:
            msg += ' ..::.. ' + self.device_string
        else:
            msg += _(' %(created)s %(name)s %(modified)s %(mod_name)s') % dict(created=_('created by'), name='Kovid Goyal', modified='modified by', mod_name='Orio Sargenti')    # ORIO

        if self.total != self.current:
            base = _('%(num)d of %(total)d books') % dict(num=self.current, total=self.total)
        else:
            base = ngettext('one book', '{} books', self.total).format(self.total)
        if self.selected > 0:
            base = ngettext('%(num)s, %(sel)d selected', '%(num)s, %(sel)d selected', self.selected) % dict(num=base, sel=self.selected)
        if self.library_total != self.total:
            base = _('{0}, {1} total').format(base, self.library_total)

        self.defmsg.setText(f'\xa0{msg}\xa0\xa0\xa0\xa0[{base}] ')
        self.clearMessage()

    def device_disconnected(self):
        self.device_string = ''
        self.set_label()

    def show_message(self, msg, timeout=0, show_notification=True):
        self.showMessage(msg, timeout)
        if self.notifier is not None and not config['disable_tray_notification'] and show_notification:
            self.notifier(msg)

    def clear_message(self):
        self.clearMessage()
# }}}


class GridViewButton(LayoutButton):  # {{{

    def __init__(self, gui):
        sc = 'Alt+Shift+G'
        LayoutButton.__init__(self, 'cover_grid', 'grid.png', _('Cover grid'), gui, shortcut=sc)
        self.set_state_to_show()
        self.action_toggle = QAction(self.icon(), _('Toggle') + ' ' + self.label, self)
        gui.addAction(self.action_toggle)
        gui.keyboard.register_shortcut('grid view toggle' + self.label, str(self.action_toggle.text()),
                                    default_keys=(sc,), action=self.action_toggle, group=_('Main window layout'))
        self.action_toggle.triggered.connect(self.toggle)
        self.action_toggle.changed.connect(self.update_shortcut)
        self.toggled.connect(self.update_state)

    @property
    def is_visible(self):
        return self.isChecked()

    def update_state(self, checked):
        if checked:
            self.set_state_to_hide()
        else:
            self.set_state_to_show()

    def save_state(self):
        gprefs['grid view visible'] = bool(self.isChecked())

    def restore_state(self):
        if gprefs.get('grid view visible', False):
            self.toggle()
# }}}


class SearchBarButton(LayoutButton):  # {{{

    def __init__(self, gui):
        sc = 'Alt+Shift+F'
        LayoutButton.__init__(self, 'search', 'search.png', _('Search bar'), gui, shortcut=sc)
        self.set_state_to_hide()
        self.action_toggle = QAction(self.icon(), _('Toggle') + ' ' + self.label, self)
        gui.addAction(self.action_toggle)
        gui.keyboard.register_shortcut('search bar toggle' + self.label, str(self.action_toggle.text()),
                                    default_keys=(sc,), action=self.action_toggle, group=_('Main window layout'))
        self.action_toggle.triggered.connect(self.toggle)
        self.action_toggle.changed.connect(self.update_shortcut)
        self.toggled.connect(self.update_state)
        self.update_shortcut()

    @property
    def is_visible(self):
        return self.isChecked()

    def update_state(self, checked):
        if checked:
            self.set_state_to_hide()
        else:
            self.set_state_to_show()

    def save_state(self):
        gprefs['search bar visible'] = bool(self.isChecked())

    def restore_state(self):
        self.setChecked(bool(gprefs.get('search bar visible', True)))
# }}}


class VLTabs(QTabBar):  # {{{

    def __init__(self, parent):
        QTabBar.__init__(self, parent)
        self.setDocumentMode(True)
        self.setDrawBase(False)
        self.setTabsClosable(gprefs['vl_tabs_closable'])
        self.setMovable(self.tabsClosable())
        self.gui = parent
        self.ignore_tab_changed = False
        self.currentChanged.connect(self.tab_changed)
        self.tabMoved.connect(self.tab_moved, type=Qt.ConnectionType.QueuedConnection)
        self.tabCloseRequested.connect(self.tab_close)
        self.update_visibility()
        self.next_action = a = QAction(self)
        a.triggered.connect(partial(self.next_tab, delta=1)), self.gui.addAction(a)
        self.previous_action = a = QAction(self)
        a.triggered.connect(partial(self.next_tab, delta=-1)), self.gui.addAction(a)
        self.gui.keyboard.register_shortcut(
            'virtual-library-tab-bar-next', _('Next Virtual library'), action=self.next_action,
            default_keys=('Ctrl+Right',),
            description=_('Switch to the next Virtual library in the Virtual library tab bar')
        )
        self.gui.keyboard.register_shortcut(
            'virtual-library-tab-bar-previous', _('Previous Virtual library'), action=self.previous_action,
            default_keys=('Ctrl+Left',),
            description=_('Switch to the previous Virtual library in the Virtual library tab bar')
        )

    def next_tab(self, delta=1):
        if self.count() > 1 and self.isVisible():
            idx = (self.currentIndex() + delta) % self.count()
            self.setCurrentIndex(idx)

    def update_visibility(self):
        cv = self.gui.current_view()
        self.setVisible(gprefs['show_vl_tabs'] and self.count() > 1 and (cv is self.gui.library_view or cv is None))

    def enable_bar(self):
        gprefs['show_vl_tabs'] = True
        self.update_visibility()
        self.gui.set_number_of_books_shown()

    def disable_bar(self):
        gprefs['show_vl_tabs'] = False
        self.update_visibility()
        self.gui.set_number_of_books_shown()

    def lock_tab(self):
        gprefs['vl_tabs_closable'] = False
        self.setTabsClosable(False)
        self.setMovable(False)
        # Workaround for Qt bug where it doesn't recalculate the tab size after locking
        for idx in range(self.count()):
            self.setTabButton(idx, QTabBar.ButtonPosition.RightSide, None)
            self.setTabButton(idx, QTabBar.ButtonPosition.LeftSide, None)

    def unlock_tab(self):
        gprefs['vl_tabs_closable'] = True
        self.setTabsClosable(True)
        self.setMovable(True)
        # ensure no button on the All books tab since it is not closeable
        for idx in range(self.count()):
            if not self.tabData(idx):
                try:
                    self.tabButton(idx, QTabBar.ButtonPosition.RightSide).setVisible(False)
                except AttributeError:
                    try:
                        self.tabButton(idx, QTabBar.ButtonPosition.LeftSide).setVisible(False)
                    except AttributeError:
                        # On some OS X machines (using native style) the tab button is
                        # on the left
                        pass
                break

    def tab_changed(self, idx):
        if self.ignore_tab_changed:
            return
        vl = str(self.tabData(idx) or '').strip() or None
        self.gui.apply_virtual_library(vl, update_tabs=False)

    def tab_moved(self, from_, to):
        self.current_db.new_api.set_pref('virt_libs_order', [str(self.tabData(i) or '') for i in range(self.count())])

    def tab_close(self, index):
        vl = str(self.tabData(index) or '')
        if vl:  # Don't allow closing the All Books tab
            self.current_db.new_api.set_pref('virt_libs_hidden', list(
                self.current_db.new_api.pref('virt_libs_hidden', ())) + [vl])
            self.removeTab(index)

    @property
    def current_db(self):
        return self.gui.current_db

    def rebuild(self):
        self.ignore_tab_changed = True
        try:
            self._rebuild()
        finally:
            self.ignore_tab_changed = False

    def _rebuild(self):
        db = self.current_db
        vl_map = db.new_api.pref('virtual_libraries', {})
        virt_libs = frozenset(vl_map)
        hidden = set(db.new_api.pref('virt_libs_hidden', ()))
        if hidden - virt_libs:
            hidden = hidden.intersection(virt_libs)
            db.new_api.set_pref('virt_libs_hidden', list(hidden))
        order = db.new_api.pref('virt_libs_order', ())
        while self.count():
            self.removeTab(0)
        current_lib = db.data.get_base_restriction_name()
        if current_lib in hidden:
            hidden.discard(current_lib)
            db.new_api.set_pref('virt_libs_hidden', list(hidden))
        current_idx = all_idx = None
        virt_libs = (set(virt_libs) - hidden) | {''}
        order = {x:i for i, x in enumerate(order)}
        for i, vl in enumerate(sorted(virt_libs, key=lambda x:(order.get(x, 0), sort_key(x)))):
            self.addTab(vl.replace('&', '&&') or _('All books'))
            sexp = vl_map.get(vl, None)
            if sexp is not None:
                self.setTabToolTip(i, _('Search expression for this Virtual library:') + '\n\n' + sexp)
            self.setTabData(i, vl)
            if vl == current_lib:
                current_idx = i
            if not vl:
                all_idx = i
        self.setCurrentIndex(all_idx if current_idx is None else current_idx)
        if current_idx is None and current_lib:
            self.setTabText(all_idx, current_lib)
        try:
            self.tabButton(all_idx, QTabBar.ButtonPosition.RightSide).setVisible(False)
        except AttributeError:
            try:
                self.tabButton(all_idx, QTabBar.ButtonPosition.LeftSide).setVisible(False)
            except AttributeError:
                # On some OS X machines (using native style) the tab button is
                # on the left
                pass
        self.update_visibility()

    def update_current(self):
        self.rebuild()

    def contextMenuEvent(self, ev):
        m = QMenu(self)
        m.addAction(QIcon.ic('sort.png'), _('Sort tabs alphabetically'), self.sort_alphabetically)
        hidden = self.current_db.new_api.pref('virt_libs_hidden')
        if hidden:
            s = m._s = m.addMenu(_('Restore hidden tabs'))
            for x in hidden:
                s.addAction(x, partial(self.restore, x))
        m.addAction(_('Hide Virtual library tabs'), self.disable_bar)
        if gprefs['vl_tabs_closable']:
            m.addAction(QIcon.ic('drm-locked.png'), _('Lock Virtual library tabs'), self.lock_tab)
        else:
            m.addAction(QIcon.ic('drm-unlocked.png'), _('Unlock Virtual library tabs'), self.unlock_tab)
        i = self.tabAt(ev.pos())
        if i > -1:
            vl = str(self.tabData(i) or '')
            if vl:
                vln = vl.replace('&', '&&')
                m.addSeparator()
                m.addAction(QIcon.ic('edit_input.png'), _('Edit "%s"') % vln, partial(self.gui.do_create_edit, name=vl))
                m.addAction(QIcon.ic('trash.png'), _('Delete "%s"') % vln, partial(self.gui.remove_vl_triggered, name=vl))
        m.exec(ev.globalPos())

    def sort_alphabetically(self):
        self.current_db.new_api.set_pref('virt_libs_order', ())
        self.rebuild()

    def restore(self, x):
        h = self.current_db.new_api.pref('virt_libs_hidden', ())
        self.current_db.new_api.set_pref('virt_libs_hidden', list(set(h) - {x}))
        self.rebuild()

# }}}


class StatusBarButton(QToolButton):

    def __init__(self, parent, action_name, pref_name, on_click):
        super().__init__(parent=parent)
        act = parent.iactions[action_name]
        self.action_name = action_name
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAutoRaise(True)
        self.setIcon(QIcon.ic(act.action_spec[1]))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setText(act.action_spec[0])
        self.setToolTip(act.action_spec[2])
        self.setVisible(gprefs[pref_name])
        parent.status_bar.addPermanentWidget(self)
        if on_click == 'menu':
            self.setMenu(act.qaction.menu())
        elif on_click == 'trigger':
            self.clicked.connect(act.qaction.trigger)
        else:
            raise ValueError(f'make_status_line_action_button: invalid on_click ({on_click}')


class LayoutMixin:  # {{{

    def __init__(self, *args, **kwargs):
        pass

    def place_layout_buttons(self):
        if getattr(self, 'layout_buttons', None):
            for x in self.layout_buttons:
                self.status_bar.removeWidget(x)
        if self.layout_container.is_wide:
            self.button_order = 'sb', 'tb', 'cb', 'gv', 'qv', 'bd'
        else:
            self.button_order = 'sb', 'tb', 'bd', 'gv', 'cb', 'qv'
        self.layout_buttons = []
        stylename = str(self.style().objectName())
        for x in self.button_order:
            if x == 'gv':
                button = self.grid_view_button
            elif x == 'sb':
                button = self.search_bar_button
            else:
                button = self.layout_container.button_for({
                    'tb': 'tag_browser', 'bd': 'book_details', 'cb': 'cover_browser', 'qv': 'quick_view'
                }[x])
            self.layout_buttons.append(button)
            button.setVisible(gprefs['show_layout_buttons'])
            if ismacos and stylename != 'Calibre':
                button.setStyleSheet('''
                        QToolButton { background: none; border:none; padding: 0px; }
                        QToolButton:checked { background: rgba(0, 0, 0, 25%); }
                ''')
        for button in reversed(self.layout_buttons):
            self.status_bar.insertPermanentWidget(2, button)
        self.layout_button.setVisible(not gprefs['show_layout_buttons'])

    def init_layout_mixin(self):
        self.vl_tabs = VLTabs(self)
        self.centralwidget.layout().addWidget(self.vl_tabs)
        self.layout_container = CentralContainer(self)
        self.centralwidget.layout().addWidget(self.layout_container)
        self.book_details = BookDetails(self.layout_container.is_wide, self)
        self.stack = QStackedWidget(self)
        self.library_view = BooksView(self)
        self.library_view.setObjectName('library_view')
        stack = QStackedWidget(self)
        self.stack.addWidget(stack)
        av = self.library_view.alternate_views
        self.pin_container = av.set_stack(stack)
        self.grid_view = GridView(self)
        self.grid_view.setObjectName('grid_view')
        av.add_view('grid', self.grid_view)
        self.tb_widget = TagBrowserWidget(self)
        self.memory_view = DeviceBooksView(self)
        self.stack.addWidget(self.memory_view)
        self.memory_view.setObjectName('memory_view')
        self.card_a_view = DeviceBooksView(self)
        self.stack.addWidget(self.card_a_view)
        self.card_a_view.setObjectName('card_a_view')
        self.card_b_view = DeviceBooksView(self)
        self.stack.addWidget(self.card_b_view)
        self.card_b_view.setObjectName('card_b_view')

        # This must use the base method to find the plugin because it hasn't
        # been fully initialized yet
        self.qv = find_plugin('Quickview')
        if self.qv and self.qv.actual_plugin_:
            self.qv = self.qv.actual_plugin_

        self.layout_container.initialize_with_gui(self, self.stack)
        self.layout_container.tag_browser_button.toggled.connect(
            self.tb_widget.set_pane_is_visible, Qt.ConnectionType.QueuedConnection)
        self.status_bar = StatusBar(self)
        self.grid_view_button = GridViewButton(self)
        self.search_bar_button = SearchBarButton(self)
        self.grid_view_button.toggled.connect(self.toggle_grid_view)
        self.search_bar_button.toggled.connect(self.toggle_search_bar)

        self.layout_button = b = QToolButton(self)
        self.layout_button_menu = m = LayoutMenu(self)
        b.setAutoRaise(True), b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setText(_('Layout')), b.setIcon(QIcon.ic('layout.png'))
        b.setToolTip(_(
            'Show and hide various parts of the calibre main window'))
        b.clicked.connect(m.toggle_visibility)
        self.status_bar.addPermanentWidget(b)

        # These must be after the layout button because it can be expanded into
        # the component buttons. Order: last is right-most.
        # The preferences status bar button isn't (yet) allowed on the status bar
        # self.sb_preferences_button = StatusBarButton(self, 'Preferences', 'show_sb_preference_button', 'trigger')
        self.sb_all_gui_actions_button = StatusBarButton(self, 'All GUI actions',
                                                         'show_sb_all_actions_button', 'menu')
        self.status_bar_extra_buttons = (self.sb_all_gui_actions_button,)

        self.status_bar.addPermanentWidget(self.jobs_button)
        self.setStatusBar(self.status_bar)
        self.status_bar.update_label.linkActivated.connect(self.update_link_clicked)

    def finalize_layout(self):
        self.status_bar.initialize(self.system_tray_icon)
        self.book_details.show_book_info.connect(self.iactions['Show Book Details'].show_book_info)
        self.book_details.files_dropped.connect(self.iactions['Add Books'].files_dropped_on_book)
        self.book_details.cover_changed.connect(self.bd_cover_changed,
                type=Qt.ConnectionType.QueuedConnection)
        self.book_details.open_cover_with.connect(self.bd_open_cover_with,
                type=Qt.ConnectionType.QueuedConnection)
        self.book_details.open_fmt_with.connect(self.bd_open_fmt_with,
                type=Qt.ConnectionType.QueuedConnection)
        self.book_details.edit_book.connect(self.bd_edit_book,
                type=Qt.ConnectionType.QueuedConnection)
        self.book_details.cover_removed.connect(self.bd_cover_removed,
                type=Qt.ConnectionType.QueuedConnection)
        self.book_details.remote_file_dropped.connect(
                self.iactions['Add Books'].remote_file_dropped_on_book,
                type=Qt.ConnectionType.QueuedConnection)
        self.book_details.open_containing_folder.connect(self.iactions['View'].view_folder_for_id)
        self.book_details.open_data_folder.connect(self.iactions['View'].view_data_folder_for_id)
        self.book_details.view_specific_format.connect(self.iactions['View'].view_format_by_id)
        self.book_details.search_requested.connect(self.set_search_string_with_append)
        self.book_details.remove_specific_format.connect(
                self.iactions['Remove Books'].remove_format_by_id)
        self.book_details.remove_metadata_item.connect(
                self.iactions['Edit Metadata'].remove_metadata_item)
        self.book_details.save_specific_format.connect(
                self.iactions['Save To Disk'].save_library_format_by_ids)
        self.book_details.restore_specific_format.connect(
            self.iactions['Remove Books'].restore_format)
        self.book_details.set_cover_from_format.connect(
            self.iactions['Edit Metadata'].set_cover_from_format)
        self.book_details.copy_link.connect(self.bd_copy_link,
                type=Qt.ConnectionType.QueuedConnection)
        self.book_details.view_device_book.connect(
                self.iactions['View'].view_device_book)
        self.book_details.manage_category.connect(self.manage_category_triggerred)
        self.book_details.find_in_tag_browser.connect(self.find_in_tag_browser_triggered)
        self.book_details.edit_identifiers.connect(self.edit_identifiers_triggerred)
        self.book_details.compare_specific_format.connect(self.compare_format)

        m = self.library_view.model()
        if m.rowCount(None) > 0:
            QTimer.singleShot(0, self.library_view.set_current_row)
            m.current_changed(self.library_view.currentIndex(),
                    self.library_view.currentIndex())
        self.library_view.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_panel(self, name):
        if name == 'search_bar':
            self.search_bar_button.setChecked(True)
            return
        self.layout_container.show_panel(name)

    def hide_panel(self, name):
        if name == 'search_bar':
            self.search_bar_button.setChecked(False)
            return
        self.layout_container.hide_panel(name)

    def set_search_string_with_append(self, expression, append=''):
        current = self.search.text().strip()
        if append:
            expr = f'{current} {append} {expression}' if current else expression
        else:
            expr = expression
        self.search.set_search_string(expr)

    def edit_identifiers_triggerred(self):
        book_id = self.library_view.current_book
        db = self.current_db.new_api
        identifiers = db.field_for('identifiers', book_id, default_value={})
        from calibre.gui2.metadata.basic_widgets import Identifiers
        d = Identifiers(identifiers, self)
        if d.exec() == QDialog.DialogCode.Accepted:
            identifiers = d.get_identifiers()
            db.set_field('identifiers', {book_id: identifiers})
            self.iactions['Edit Metadata'].refresh_books_after_metadata_edit({book_id})

    def manage_category_triggerred(self, field, value):
        if field and value:
            if field == 'authors':
                self.do_author_sort_edit(self, value, select_sort=False,
                                         select_link=False, lookup_author=True)
            elif field:
                self.do_tags_list_edit(value, field)

    def find_in_tag_browser_triggered(self, field, value):
        if field and value:
            tb = self.tb_widget
            tb.set_focus_to_find_box()
            tb.item_search.lineEdit().setText(field + ':=' + value)
            tb.do_find()

    def toggle_grid_view(self, show):
        self.library_view.alternate_views.show_view('grid' if show else None)
        self.sort_button.setVisible(show)

    def toggle_search_bar(self, show):
        self.search_bar.setVisible(show)
        if show:
            self.search.setFocus(Qt.FocusReason.OtherFocusReason)

    def bd_cover_changed(self, id_, cdata):
        self.library_view.model().db.set_cover(id_, cdata)
        self.refresh_cover_browser()

    def bd_open_cover_with(self, book_id, entry):
        cpath = self.current_db.new_api.format_abspath(book_id, '__COVER_INTERNAL__')
        if cpath:
            if entry is None:
                pm = QPixmap()
                pm.load(cpath)
                pm.setDevicePixelRatio(self.devicePixelRatioF())
                if pm.isNull():
                    open_local_file(cpath)
                else:
                    from calibre.gui2.image_popup import ImageView
                    iv = ImageView(QApplication.instance().focusWindow(), pm, QUrl.fromLocalFile(cpath), geom_name='book_details_image_view')
                    iv(use_exec=True)
                return
            from calibre.gui2.open_with import run_program
            run_program(entry, cpath, self)

    def bd_open_fmt_with(self, book_id, fmt, entry):
        path = self.current_db.new_api.format_abspath(book_id, fmt)
        if path:
            from calibre.gui2.open_with import run_program
            run_program(entry, path, self)
        else:
            fmt = fmt.upper()
            error_dialog(self, _('No %s format') % fmt, _(
                'The book {0} does not have the {1} format').format(
                    self.current_db.new_api.field_for('title', book_id, default_value=_('Unknown')),
                    fmt), show=True)

    def bd_edit_book(self, book_id, fmt):
        from calibre.gui2.widgets import BusyCursor
        with BusyCursor():
            self.iactions['Tweak ePub'].ebook_edit_format(book_id, fmt)

    def open_with_action_triggerred(self, fmt, entry, *args):
        book_id = self.library_view.current_book
        if book_id is not None:
            if fmt == 'cover_image':
                self.bd_open_cover_with(book_id, entry)
            else:
                self.bd_open_fmt_with(book_id, fmt, entry)

    def bd_cover_removed(self, id_):
        self.library_view.model().db.remove_cover(id_, commit=True,
                notify=False)
        self.refresh_cover_browser()

    def bd_copy_link(self, url):
        if url:
            QApplication.clipboard().setText(url)

    def compare_format(self, book_id, fmt):
        db = self.current_db.new_api
        ofmt = fmt
        if fmt.startswith('ORIGINAL_'):
            fmt = fmt.partition('_')[-1]
        else:
            ofmt = 'ORIGINAL_' + fmt
        path1, path2 = db.format_abspath(book_id, ofmt), db.format_abspath(book_id, fmt)
        from calibre.gui2.tweak_book.diff.main import compare_books
        compare_books(path1, path2, parent=self, revert_msg=_('Restore %s') % ofmt, revert_callback=partial(
            self.iactions['Remove Books'].restore_format, book_id, ofmt), names=(ofmt, fmt))

    def save_layout_state(self):
        for x in ('library', 'memory', 'card_a', 'card_b'):
            getattr(self, x+'_view').save_state()
        self.layout_container.write_settings()
        self.grid_view_button.save_state()
        self.search_bar_button.save_state()

    def read_layout_settings(self):
        # View states are restored automatically when set_database is called
        self.layout_container.read_settings()
        self.book_details.change_layout(self.layout_container.is_wide)
        self.place_layout_buttons()
        self.grid_view_button.restore_state()
        self.search_bar_button.restore_state()

    def update_status_bar(self, *args):
        v = self.current_view()
        selected = len(v.selectionModel().selectedRows())
        library_total, total, current = v.model().counts()
        self.status_bar.update_state(library_total, total, current, selected)
# }}}
