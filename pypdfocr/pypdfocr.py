#!/usr/bin/env python2.7
# Copyright 2013 Virantha Ekanayake All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
    Make scanned PDFs searchable using Tesseract-OCR and autofile them
.. automodule:: pypdfocr
    :private-members:
"""

import argparse
import glob
import logging
import multiprocessing
import os
import smtplib
import sys
import time
import traceback
from functools import wraps

import yaml


from .pypdfocr_multiprocessing import Popen
from .pypdfocr_pdf import PyPdf
from .pypdfocr_tesseract import PyTesseract
from .pypdfocr_gs import PyGs
from .pypdfocr_watcher import PyPdfWatcher
from .pypdfocr_pdffiler import PyPdfFiler
from .pypdfocr_filer_dirs import PyFilerDirs
from .pypdfocr_filer_evernote import ENABLED as evernote_enabled
from .pypdfocr_filer_evernote import PyFilerEvernote
from .pypdfocr_preprocess import PyPreprocess
from .version import __version__


def setup_logging(level=logging.INFO):
    logging.basicConfig(level=logging.DEBUG, format='%(level)s - %(message)s')


def error(text):
    """Print error message and terminate."""
    print("ERROR: %s" % text)
    sys.exit(-1)


def retry(count=5, exc_type=Exception):
    """Decorator to retry multiple times."""
    def decorator(func):
        """Decorator to wrap function giving multiple attempts before failing.
        """
        @wraps(func)
        def result(*args, **kwargs):
            """Function to do the actual calling and catching."""
            err = exc_type
            for _ in range(count):
                try:
                    return func(*args, **kwargs)
                except exc_type as raised_err:
                    err = raised_err
            raise err
        return result
    return decorator


@retry(count=6, exc_type=IOError)
def open_file_with_timeout(parser, arg):
    """Attempt to open a file multiple times before failing"""
    return open(arg, 'r')


class PyPDFOCR(object):
    """
        The main class.  Performs the following functions:

        * Parses command line options
        * Optionally just watches a directory for new PDF's to OCR; once a file appears, it does the
           next step.
        * Runs a single file conversion:
            * Runs ghostscript to get tiff/jpg
            * Runs Tesseract-OCR to do the actual OCR
            * Takes the HOCR from Tesseract and creates a new PDF with the text overlay
        * Files the OCR'ed file in the proper place if specified
        * Files the original file if specified
    """

    def __init__(self):
        """ Initializes the GhostScript, Tesseract, and PDF helper classes.
        """
        self.config = None
        self.gs = None
        self.ts = None
        self.preprocess = None
        self.filer = None
        self.pdf_filer = None

    @staticmethod
    def _get_config_file(config_file):
        """
           Read in the yaml config file

           :param config_file: Configuration file (YAML format)
           :type config_file: file
           :returns: dict of yaml file
           :rtype: dict
        """
        with config_file:
            myconfig = yaml.load(config_file)
        return myconfig

    def get_options(self, argv):
        """
            Parse the command-line options and set the following object properties:

            :param argv: usually just sys.argv[1:]
            :returns: A Namespace containing the config options

            :ivar debug: Enable logging debug statements
            :ivar verbose: Enable verbose logging
            :ivar enable_filing: Whether to enable post-OCR filing of PDFs
            :ivar pdf_filename: Filename for single conversion mode
            :ivar watch_dir: Directory to watch for files to convert
            :ivar config: Dict of the config file
            :ivar watch: Whether folder watching mode is turned on
            :ivar enable_evernote: Enable filing to evernote
        """

        parser = argparse.ArgumentParser(
            description=(
                "Convert scanned PDFs into their OCR equivalent."
                "  Depends on GhostScript and Tesseract-OCR being installed."),
            epilog=(
                "PyPDFOCR version %s (Copyright 2013 Virantha Ekanayake)"
                % __version__)
        )

        parser.add_argument(
            '-c', '--config', type=lambda x: open_file_with_timeout(parser, x),
            dest='configfile', help='Configuration file for defaults and PDF filing')
        
        args, argv = parser.parse_known_args(argv)

        # Parse configuration file (YAML) if specified
        if args.configfile:
            config = self._get_config_file(args.configfile)
            logging.debug("Read config from %s", str(args.configfile))
            logging.debug(config)
        else:
            config = {}

        parser.add_argument('-d', '--debug', action='store_true',
                            default=False, dest='debug', help='Turn on debugging')

        parser.add_argument('-v', '--verbose', action='store_true',
                            default=False, dest='verbose', help='Turn on verbose mode')

        parser.add_argument('-m', '--mail', action='store_true',
                            default=False, dest='mail', help='Send email after conversion')

        parser.add_argument('-l', '--lang',
                            default='eng', dest='lang', help='Language(default eng)')

        # Deprecating skip_preprocess to make skipping the default (always true).
        # Tesseract 3.04 is so much better now at handling non-ideal inputs and lines
        preproc = parser.add_mutually_exclusive_group()
        preproc.add_argument('--preprocess', action='store_false',
                            dest='skip_preprocess',
                            help='Enable preprocessing.' \
                            '  Not really useful now with improved Tesseract 3.04+')

        preproc.add_argument('--skip-preprocess', action='store_true',
                            dest='skip_preprocess',
                            help='DEPRECATED: Preprocessing is skipped by default.')

        #---------
        # Single or watch mode
        #--------
        single_or_watch_group = parser.add_mutually_exclusive_group(required=True)
        # Positional argument for single file conversion
        single_or_watch_group.add_argument("pdf_filename", nargs="?",
                                           help="Scanned pdf file to OCR")
        # Watch directory for watch mode
        single_or_watch_group.add_argument(
            '-w', '--watch', dest='watch_dir',
            help='Watch given directory and run ocr automatically until terminated')

        #-----------
        # Filing options
        #----------
        filing_group = parser.add_argument_group(title="Filing options")
        filing_group.add_argument(
            '-f', '--file', action='store_true', default=False,
            dest='enable_filing', help='Enable filing of converted PDFs')
        filing_group.add_argument(
            '-e', '--evernote', action='store_true',
            default=False, dest='enable_evernote', help='Enable filing to Evernote.')
        filing_group.add_argument(
            '-n', action='store_true', default=False, dest='match_using_filename',
            help='Use filename to match if contents did not match anything, '
                 'before filing to default folder')

        # Add sub-section defaults which can be set in config file
        parser.set_defaults(**{
            'ghostscript': {},
            'tesseract': {},
            'preprocess': {},
            'evernote': {},
            'email': {}
            })

        if config:
            parser.set_defaults(**config)        

        args = parser.parse_args(argv)

        args.enable_email = args.mail

        if args.debug:
            setup_logging(logging.DEBUG)

        # Evernote filing does not work in py3
        if args.enable_evernote and not evernote_enabled:
            logging.warning("Evernote filing disabled, could not find evernote"
                            " API. Evernote not available in py3.")
            args.enable_evernote = False

        args.enable_filing = bool(args.enable_filing or args.enable_evernote)
        #TODO: Move evernote config checking into evernote module
        # if args.enable_filing or self.enable_evernote:
        #     self.enable_filing = True
            # if not args.evernote:
            #     error("Please specify a configuration file(CONFIGFILE) to enable filing")
        # else:
        #     self.enable_filing = False

        args.watch = bool(args.watch_dir)

        # TODO: Move email config checking into email module
        # if self.enable_email and not args.email:
        #     parser.error("Please specify a configuration file(CONFIGFILE) to enable email")

        return args

    @staticmethod
    def _clean_up_files(files):
        """
            Helper function to delete files
            :param files: List of files to delete
            :type files: list
            :returns: None
        """
        for fname in files:
            try:
                os.remove(fname)
            except OSError:
                logging.debug("Error removing file %s .... continuing", fname)

    def _setup_filing(self):
        """
            Instance the proper PyFiler object (either
            :class:`pypdfocr.pypdfocr_filer_dirs.PyFilerDirs` or
            :class:`pypdfocr.pypdfocr_filer_evernote.PyFilerEvernote`)

            TODO: Make this more generic to allow third-party plugin filing objects

            :ivar filer: :class:`pypdfocr.pypdfocr_filer.PyFiler` PyFiler
               subclass object that is instantiated.
            :ivar pdf_filer: :class:`pypdfocr.pypdfocr_pdffiler.PyPdfFiler`
               object to help with PDF reading.
            :returns: Nothing

        """
        # Look at self.config and create a self.pdf_filer object

        # --------------------------------------------------
        # Some sanity checks
        # --------------------------------------------------
        assert self.config and self.config.enable_filing
        try:
            target_folder = os.path.abspath(self.config.target_folder)
            default_folder = os.path.abspath(self.config.default_folder)
        except AttributeError:
            error("target_folder and default_folder must be specified in "
                  "config file.")

        try:
            original_move_folder = os.path.abspath(
                self.config.original_move_folder)
        except AttributeError:
            original_move_folder = None
        else:
            if not os.path.exists(original_move_folder):
                os.makedirs(original_move_folder)
        # --------------------------------------------------
        # Start the filing object
        # --------------------------------------------------
        if self.config.enable_evernote:
            self.filer = PyFilerEvernote(self.config.evernote_developer_token)
        else:
            self.filer = PyFilerDirs()

        self.filer.target_folder = target_folder
        self.filer.default_folder = default_folder
        self.filer.original_move_folder = original_move_folder

        self.pdf_filer = PyPdfFiler(self.filer)
        if self.config.match_using_filename:
            print("Matching using filename as a fallback to pdf contents")
            self.pdf_filer.file_using_filename = True

        # ------------------------------
        # Add all the folder names with associated keywords
        # to the filer object
        # ------------------------------
        keyword_count = 0
        folder_count = 0
        if 'folders' in self.config:
            for folder, keywords in self.config.folders.items():
                folder_count += 1
                keyword_count += len(keywords)
                # Make sure keywords are lower-cased before adding
                keywords = [str(x).lower() for x in keywords]
                self.filer.add_folder_target(folder, keywords)

        print("Filing of PDFs is enabled")
        print(" - %d target filing folders" % (folder_count))
        print(" - %d keywords" % (keyword_count))

    def _setup_external_tools(self):
        """
            Instantiate the external tool wrappers with their config dicts
        """
        logging.error(self.config)
        self.gs = PyGs(self.config.ghostscript)
        self.ts = PyTesseract(self.config.tesseract)
        self.pdf = PyPdf(self.gs)
        self.preprocess = PyPreprocess(self.config.preprocess)
        return

    def run_conversion(self, pdf_filename):
        """
            Does the following:

            - Convert the PDF using GhostScript to TIFF and JPG
            - Run Tesseract on the TIFF to extract the text into HOCR (html)
            - Use PDF generator to overlay the text on the JPG and output a new PDF
            - Clean up temporary image files

            :param pdf_filename: Scanned PDF
            :type pdf_filename: string
            :returns: OCR'ed PDF
            :rtype: filename string
        """
        print("Starting conversion of %s" % pdf_filename)
        try:
            # Make the images for Tesseract
            img_dpi, glob_img_filename = self.gs.make_img_from_pdf(pdf_filename)

            fns = glob.glob(glob_img_filename)

        except Exception:
            raise

        try:
            # Preprocess
            if not self.config.skip_preprocess:
                preprocess_imagefilenames = self.preprocess.preprocess(fns)
            else:
                logging.info("Skipping preprocess step")
                preprocess_imagefilenames = fns
            # Run teserract
            self.ts.lang = self.config.lang
            hocr_filenames = self.ts.make_hocr_from_pnms(preprocess_imagefilenames)

            # Generate new pdf with overlayed text
            ocr_pdf_filename = self.pdf.overlay_hocr_pages(
                img_dpi, hocr_filenames, pdf_filename)

        finally:
            # Clean up the files
            time.sleep(1)
            if not self.config.debug:
                # Need to clean up the original image files before preprocessing
                if "fns" in locals(): # Have to check if this was set before exception raised
                    logging.info("Cleaning up %s", fns)
                    self._clean_up_files(fns)

                # Have to check if this was set before exception raised
                if "preprocess_imagefilenames" in locals():
                    logging.info("Cleaning up %s", preprocess_imagefilenames)
                    # splat the hocr_filenames as it is a list of pairs
                    self._clean_up_files(preprocess_imagefilenames)
                    for ext in [".hocr", ".html", ".txt"]:
                        fns_to_remove = [
                            os.path.splitext(fn)[0] + ext for fn in preprocess_imagefilenames]
                        logging.info("Cleaning up %s", fns_to_remove)
                        # splat the hocr_filenames as it is a list of pairs
                        self._clean_up_files(fns_to_remove)
                    # clean up the hocr input (jpg) and output (html) files
                    # self._clean_up_files(itertools.chain(*hocr_filenames))
                    # splat the hocr_filenames as it is a list of pairs
                    # Seems like newer tessearct > 3.03 is creating .txt files with the OCR text
                    # self._clean_up_files([x[1].replace(".hocr", ".txt") for x in hocr_filenames])


        print("Completed conversion successfully to %s" % ocr_pdf_filename)
        return ocr_pdf_filename

    def file_converted_file(self, ocr_pdffilename, original_pdffilename):
        """ move the converted filename to its destination directory.  Optionally also
            moves the original PDF.

            :param ocr_pdffilename: Converted PDF file
            :type ocr_pdffilename: filename string
            :param original_pdffilename: Original scanned PDF file
            :type original_pdffilename: filename string
            :returns: Target folder name
            "rtype: string
        """
        filed_path = self.pdf_filer.move_to_matching_folder(ocr_pdffilename)
        print("Filed %s to %s as %s" %
              (ocr_pdffilename, os.path.dirname(filed_path), os.path.basename(filed_path)))

        tgt_path = self.pdf_filer.file_original(original_pdffilename)
        if tgt_path != original_pdffilename:
            print("Filed original file %s to %s as %s" %
                  (original_pdffilename, os.path.dirname(tgt_path), os.path.basename(tgt_path)))
        return os.path.dirname(filed_path)

    def _send_email(self, infilename, outfilename, filing):
        """
            Send email using smtp
        """
        print("Sending email status")
        from_addr = self.config.mail_from_addr
        to_addr_list = self.config.mail_to_list
        smtpserver = self.config.mail_smtp_server
        login = self.config.mail_smtp_login
        password = self.config.mail_smtp_password

        subject = "PyPDFOCR converted: %s" % (os.path.basename(outfilename))
        header = 'From: %s\n' % login
        header += 'To: %s\n' % ','.join(to_addr_list)
        header += 'Subject: %s\n\n' % subject
        message = """
        PyPDFOCR Conversion:
        --------------------
        Original file: %s
        Converted file: %s
        Filing: %s
        """ % (infilename, outfilename, filing)
        message = header + message

        server = smtplib.SMTP(smtpserver)
        server.starttls()
        server.login(login, password)
        server.sendmail(from_addr, to_addr_list, message)
        server.quit()

    def go(self, argv):
        """
            The main entry point into PyPDFOCR

            #. Parses options
            #. If filing is enabled, call :func:`_setup_filing`
            #. If watch is enabled, start the watcher
            #. :func:`run_conversion`
            #. if filing is enabled, call :func:`file_converted_file`
        """
        # Read the command line options
        self.config = self.get_options(argv)
        # Setup tesseract and ghostscript
        self._setup_external_tools()

        # Setup the pdf filing if enabled
        if self.config.enable_filing:
            self._setup_filing()

        # Do the actual conversion followed by optional filing and email
        if self.config.watch_dir:
            logging.info("Starting to watch %s", self.config.watch_dir)
            while True:  # Make sure the watcher doesn't terminate
                try:
                    py_watcher = PyPdfWatcher(self.config.watch_dir,
                                              self.config.get('watch'))
                    for pdf_filename in py_watcher.start():
                        self._convert_and_file_email(pdf_filename)
                except KeyboardInterrupt:
                    break
                except Exception as err:
                    print(traceback.print_exc(err))
                    py_watcher.stop()
        else:
            self._convert_and_file_email(self.config.pdf_filename)

    def _convert_and_file_email(self, pdf_filename):
        """
            Helper function to run the conversion, then do the optional filing,
            and optional emailing.
        """
        ocr_pdffilename = self.run_conversion(pdf_filename)
        if self.config.enable_filing:
            filing = self.file_converted_file(ocr_pdffilename, pdf_filename)
        else:
            filing = "None"

        if self.config.enable_email:
            self._send_email(pdf_filename, ocr_pdffilename, filing)


def main(): # pragma: no cover
    """Run the program"""
    setup_logging()
    multiprocessing.freeze_support()
    script = PyPDFOCR()
    script.go(sys.argv[1:])


if __name__ == '__main__':
    main()
