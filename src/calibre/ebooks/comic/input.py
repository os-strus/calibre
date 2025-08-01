__license__   = 'GPL v3'
__copyright__ = '2008, Kovid Goyal kovid@kovidgoyal.net'
__docformat__ = 'restructuredtext en'

'''
Based on ideas from comiclrf created by FangornUK.
'''

import os
import time
import traceback

from calibre import extract, prints, walk
from calibre.constants import filesystem_encoding
from calibre.ptempfile import PersistentTemporaryDirectory
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.icu import numeric_sort_key
from calibre.utils.ipc.job import ParallelJob
from calibre.utils.ipc.server import Server
from polyglot.queue import Empty

# If the specified screen has either dimension larger than this value, no image
# rescaling is done (we assume that it is a tablet output profile)
MAX_SCREEN_SIZE = 3000


def extract_comic(path_to_comic_file):
    '''
    Un-archive the comic file.
    '''
    tdir = PersistentTemporaryDirectory(suffix='_comic_extract')
    if not isinstance(tdir, str):
        # Needed in case the zip file has wrongly encoded unicode file/dir
        # names
        tdir = tdir.decode(filesystem_encoding)
    extract(path_to_comic_file, tdir)
    for x in walk(tdir):
        bn = os.path.basename(x)
        nbn = clean_ascii_chars(bn.replace('#', '_'))
        if nbn and nbn != bn:
            os.rename(x, os.path.join(os.path.dirname(x), nbn))
    return tdir


def generate_entries_from_dir(path):
    from functools import partial

    from calibre import walk
    ans = {}
    for x in walk(path):
        x = os.path.abspath(x)
        ans[x] = partial(os.path.getmtime, x)
    return ans


def find_pages(dir_or_items, sort_on_mtime=False, verbose=False):
    '''
    Find valid comic pages in a previously un-archived comic.

    :param dir_or_items: Directory in which extracted comic lives or a dict of paths to function getting mtime
    :param sort_on_mtime: If True sort pages based on their last modified time.
                          Otherwise, sort alphabetically.
    '''
    from calibre.libunzip import comic_exts
    items = generate_entries_from_dir(dir_or_items) if isinstance(dir_or_items, str) else dir_or_items
    sep_counts = set()
    pages = []
    for path in items:
        if '__MACOSX' in path:
            continue
        ext = path.rpartition('.')[2].lower()
        if ext in comic_exts:
            sep_counts.add(path.replace('\\', '/').count('/'))
            pages.append(path)
    # Use the full path to sort unless the files are in folders of different
    # levels, in which case simply use the filenames.
    basename = os.path.basename if len(sep_counts) > 1 else lambda x: x
    if sort_on_mtime:
        def key(x):
            return items[x]()
    else:
        def key(x):
            return numeric_sort_key(basename(x))

    pages.sort(key=key)
    if verbose:
        prints('Found comic pages...')
        try:
            base = os.path.commonpath(pages)
        except ValueError:
            pass
        else:
            prints('\t'+'\n\t'.join([os.path.relpath(p, base) for p in pages]))
    return pages


class PageProcessor(list):  # {{{

    '''
    Contains the actual image rendering logic. See :method:`render` and
    :method:`process_pages`.
    '''

    def __init__(self, path_to_page, dest, opts, num):
        list.__init__(self)
        self.path_to_page = path_to_page
        self.opts         = opts
        self.num          = num
        self.dest         = dest
        self.rotate       = False
        self.src_img_was_grayscale = False
        self.src_img_format = None
        self.render()

    def render(self):
        from qt.core import QImage

        from calibre.utils.filenames import make_long_path_useable
        from calibre.utils.img import crop_image, image_from_data, scale_image
        with open(make_long_path_useable(self.path_to_page), 'rb') as f:
            img = image_from_data(f.read())
        width, height = img.width(), img.height()
        if self.num == 0:  # First image so create a thumbnail from it
            with open(os.path.join(self.dest, 'thumbnail.png'), 'wb') as f:
                f.write(scale_image(img, as_png=True)[-1])
        self.src_img_format = img.format()
        self.src_img_was_grayscale = self.src_img_format in (QImage.Format.Format_Grayscale8, QImage.Format.Format_Grayscale16) or (
            img.format() == QImage.Format.Format_Indexed8 and img.allGray())
        self.pages = [img]
        if width > height:
            if self.opts.landscape:
                self.rotate = True
            else:
                half = width // 2
                split1 = crop_image(img, 0, 0, half, height)
                split2 = crop_image(img, half, 0, width - half, height)
                self.pages = [split2, split1] if self.opts.right2left else [split1, split2]
        self.process_pages()

    def process_pages(self):
        from qt.core import QImage

        from calibre.utils.img import (
            add_borders_to_image,
            despeckle_image,
            gaussian_sharpen_image,
            image_to_data,
            normalize_image,
            quantize_image,
            remove_borders_from_image,
            resize_image,
            rotate_image,
        )
        for i, img in enumerate(self.pages):
            if self.rotate:
                img = rotate_image(img, -90)

            if not self.opts.disable_trim:
                img = remove_borders_from_image(img)

            # Do the Photoshop "Auto Levels" equivalent
            if not self.opts.dont_normalize:
                img = normalize_image(img)
            sizex, sizey = img.width(), img.height()

            SCRWIDTH, SCRHEIGHT = self.opts.output_profile.comic_screen_size

            try:
                if self.opts.comic_image_size:
                    SCRWIDTH, SCRHEIGHT = map(int, [x.strip() for x in
                        self.opts.comic_image_size.split('x')])
            except Exception:
                pass  # Ignore

            if self.opts.keep_aspect_ratio:
                # Preserve the aspect ratio by adding border
                aspect = float(sizex) / float(sizey)
                if aspect <= (float(SCRWIDTH) / float(SCRHEIGHT)):
                    newsizey = SCRHEIGHT
                    newsizex = int(newsizey * aspect)
                    deltax = (SCRWIDTH - newsizex) // 2
                    deltay = 0
                else:
                    newsizex = SCRWIDTH
                    newsizey = int(newsizex // aspect)
                    deltax = 0
                    deltay = (SCRHEIGHT - newsizey) // 2
                if newsizex < MAX_SCREEN_SIZE and newsizey < MAX_SCREEN_SIZE:
                    # Too large and resizing fails, so better
                    # to leave it as original size
                    img = resize_image(img, newsizex, newsizey)
                    img = add_borders_to_image(img, left=deltax, right=deltax, top=deltay, bottom=deltay)
            elif self.opts.wide:
                # Keep aspect and Use device height as scaled image width so landscape mode is clean
                aspect = float(sizex) / float(sizey)
                screen_aspect = float(SCRWIDTH) / float(SCRHEIGHT)
                # Get dimensions of the landscape mode screen
                # Add 25px back to height for the battery bar.
                wscreenx = SCRHEIGHT + 25
                wscreeny = int(wscreenx // screen_aspect)
                if aspect <= screen_aspect:
                    newsizey = wscreeny
                    newsizex = int(newsizey * aspect)
                    deltax = (wscreenx - newsizex) // 2
                    deltay = 0
                else:
                    newsizex = wscreenx
                    newsizey = int(newsizex // aspect)
                    deltax = 0
                    deltay = (wscreeny - newsizey) // 2
                if newsizex < MAX_SCREEN_SIZE and newsizey < MAX_SCREEN_SIZE:
                    # Too large and resizing fails, so better
                    # to leave it as original size
                    img = resize_image(img, newsizex, newsizey)
                    img = add_borders_to_image(img, left=deltax, right=deltax, top=deltay, bottom=deltay)
            else:
                if SCRWIDTH < MAX_SCREEN_SIZE and SCRHEIGHT < MAX_SCREEN_SIZE:
                    img = resize_image(img, SCRWIDTH, SCRHEIGHT)

            if not self.opts.dont_sharpen:
                img = gaussian_sharpen_image(img, 0.0, 1.0)

            if self.opts.despeckle:
                img = despeckle_image(img)

            img_is_grayscale = self.src_img_was_grayscale
            if not self.opts.dont_grayscale:
                img = img.convertToFormat(QImage.Format.Format_Grayscale16)
                img_is_grayscale = True

            if self.opts.output_format.lower() == 'png':
                if self.opts.colors:
                    img = quantize_image(img, max_colors=min(256, self.opts.colors))
                elif img_is_grayscale:
                    uses_256_colors = self.src_img_format in (QImage.Format.Format_Indexed8, QImage.Format.Format_Grayscale8)
                    final_fmt = QImage.Format.Format_Indexed8 if uses_256_colors else QImage.Format.Format_Grayscale16
                    if img.format() != final_fmt:
                        img = img.convertToFormat(final_fmt)
            dest = f'{self.num}_{i}.{self.opts.output_format}'
            dest = os.path.join(self.dest, dest)
            with open(dest, 'wb') as f:
                f.write(image_to_data(img, fmt=self.opts.output_format))
            self.append(dest)
# }}}


def render_pages(tasks, dest, opts, notification=lambda x, y: x):
    '''
    Entry point for the job server.
    '''
    failures, pages = [], []
    for num, path in tasks:
        try:
            pages.extend(PageProcessor(path, dest, opts, num))
            msg = _('Rendered %s')%path
        except Exception:
            failures.append(path)
            msg = _('Failed %s')%path
            if opts.verbose:
                msg += '\n' + traceback.format_exc()
        prints(msg)
        notification(0.5, msg)

    return pages, failures


class Progress:

    def __init__(self, total, update):
        self.total  = total
        self.update = update
        self.done   = 0

    def __call__(self, percent, msg=''):
        self.done += 1
        # msg = msg%os.path.basename(job.args[0])
        self.update(float(self.done)/self.total, msg)


def process_pages(pages, opts, update, tdir):
    '''
    Render all identified comic pages.
    '''
    progress = Progress(len(pages), update)
    server = Server()
    jobs = []
    tasks = [(p, os.path.join(tdir, os.path.basename(p))) for p in pages]
    tasks = server.split(pages)
    for task in tasks:
        jobs.append(ParallelJob('render_pages', '', progress,
                                args=[task, tdir, opts]))
        server.add_job(jobs[-1])
    while True:
        time.sleep(1)
        running = False
        for job in jobs:
            while True:
                try:
                    x = job.notifications.get_nowait()
                    progress(*x)
                except Empty:
                    break
            job.update()
            if not job.is_finished:
                running = True
        if not running:
            break
    server.close()
    ans, failures = [], []

    for job in jobs:
        if job.failed or job.result is None:
            raise Exception(_('Failed to process comic: \n\n%s')%
                    job.log_file.read())
        pages, failures_ = job.result
        ans += pages
        failures += failures_
    return ans, failures
