# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2017, James R. Barlow (https://github.com/jbarlow83/)


from pikepdf import Object, ObjectType, PdfError
from .encryption import Permissions, Encryption, EncryptionInfo
from .image import PdfImage, PdfInlineImage, UnsupportedImageTypeError
from .matrix import PdfMatrix
from .metadata import PdfMetadata


def parse_content_stream(page_or_stream, operators=''):
    """
    Parse a PDF content stream into a sequence of instructions.

    A PDF content stream is list of instructions that describe where to render
    the text and graphics in a PDF. This is the starting point for analyzing
    PDFs.

    If the input is a page and page.Contents is an array, then the content
    stream is automatically treated as one coalesced stream.

    Each instruction contains at least one operator and zero or more operands.

    Args:
        page_or_stream (pikepdf.Object): A page object, or the content
            stream attached to another object such as a Form XObject.
        operators (str): A space-separated string of operators to whitelist.
            For example 'q Q cm Do' will return only operators
            that pertain to drawing images. Use 'BI ID EI' for inline images.
            All other operators and associated tokens are ignored. If blank,
            all tokens are accepted.

    Returns:
        list: List of ``(operands, command)`` tuples where ``command`` is an
            operator (str) and ``operands`` is a tuple of str; the PDF drawing
            command and the command's operands, respectively.

    Example:

        >>> pdf = pikepdf.Pdf.open(input_pdf)
        >>> page = pdf.pages[0]
        >>> for operands, command in parse_content_stream(page):
        >>>     print(command)

    """

    if not isinstance(page_or_stream, Object):
        raise TypeError("stream must a PDF object")

    if (
        page_or_stream._type_code != ObjectType.stream
        and page_or_stream.get('/Type') != '/Page'
    ):
        raise TypeError("parse_content_stream called on page or stream object")

    try:
        if page_or_stream.get('/Type') == '/Page':
            page = page_or_stream
            instructions = page._parse_page_contents_grouped(operators)
        else:
            stream = page_or_stream
            instructions = Object._parse_stream_grouped(stream, operators)
    except PdfError as e:
        # This is the error message for qpdf >= 7.0. It was different in 6.x
        # but we no longer support 6.x
        if 'ignoring non-stream while parsing' in str(e):
            raise TypeError("parse_content_stream called on non-stream Object")
        raise e from e

    return instructions
