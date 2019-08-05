# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2017, James R. Barlow (https://github.com/jbarlow83/)

import struct
from abc import ABC, abstractmethod
from decimal import Decimal
from io import BytesIO
from itertools import zip_longest
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from zlib import decompress, error as ZlibError

from .. import Array, Dictionary, Name, Object, PdfError, Stream


class DependencyError(Exception):
    pass


class UnsupportedImageTypeError(Exception):
    pass


def array_str(value):
    if isinstance(value, (list, Array)):
        return [str(item) for item in value]
    if isinstance(value, Name):
        return [str(value)]
    raise NotImplementedError(value)


def array_str_colorspace(value):
    if isinstance(value, (list, Array)):
        items = [item for item in value]
        if len(items) == 4 and items[0] == '/Indexed':
            result = [str(items[n]) for n in range(3)]
            result.append(bytes(items[3]))
            return result
        if len(items) == 2 and items[0] == '/ICCBased':
            result = [str(items[0]), items[1]]
            return result
        return array_str(items)

    return array_str(value)


def dict_or_array_dict(value):
    if isinstance(value, list):
        return value
    if isinstance(value, Dictionary):
        return [value.as_dict()]
    if isinstance(value, Array):
        return [v.as_list() for v in value]
    raise NotImplementedError(value)


def metadata_from_obj(obj, name, type_, default):
    val = getattr(obj, name, default)
    try:
        return type_(val)
    except TypeError:
        if val is None:
            return None
    raise NotImplementedError('Metadata access for ' + name)


class PdfImageBase(ABC):

    SIMPLE_COLORSPACES = ('/DeviceRGB', '/DeviceGray', '/CalRGB', '/CalGray')

    @abstractmethod
    def _metadata(self, name, type_, default):
        pass

    @property
    def width(self):
        """Width of the image data in pixels"""
        return self._metadata('Width', int, None)

    @property
    def height(self):
        """Height of the image data in pixels"""
        return self._metadata('Height', int, None)

    @property
    def image_mask(self):
        """``True`` if this is an image mask"""
        return self._metadata('ImageMask', bool, False)

    @property
    def _bpc(self):
        """Bits per component for this image (low-level)"""
        return self._metadata('BitsPerComponent', int, None)

    @property
    def _colorspaces(self):
        """Colorspace (low-level)"""
        return self._metadata('ColorSpace', array_str_colorspace, [])

    @property
    def filters(self):
        """List of names of the filters that we applied to encode this image"""
        return self._metadata('Filter', array_str, [])

    @property
    def decode_parms(self):
        """List of the /DecodeParms, arguments to filters"""
        return self._metadata('DecodeParms', dict_or_array_dict, [])

    @property
    def colorspace(self):
        """PDF name of the colorspace that best describes this image"""
        if self.image_mask:
            return None  # Undefined for image masks
        if self._colorspaces:
            if self._colorspaces[0] in self.SIMPLE_COLORSPACES:
                return self._colorspaces[0]
            if self._colorspaces[0] in ('/DeviceCMYK', '/ICCBased'):
                return self._colorspaces[0]
            if (
                self._colorspaces[0] == '/Indexed'
                and self._colorspaces[1] in self.SIMPLE_COLORSPACES
            ):
                return self._colorspaces[1]
        raise NotImplementedError(
            "not sure how to get colorspace: " + repr(self._colorspaces)
        )

    @property
    def bits_per_component(self):
        """Bits per component of this image"""
        if self._bpc is None:
            return 1 if self.image_mask else 8
        return self._bpc

    @property
    @abstractmethod
    def is_inline(self):
        pass

    @property
    @abstractmethod
    def icc(self):
        pass

    @property
    def indexed(self):
        """``True`` if the image has a defined color palette"""
        return '/Indexed' in self._colorspaces

    @property
    def size(self):
        """Size of image as (width, height)"""
        return self.width, self.height

    @property
    def mode(self):
        """``PIL.Image.mode`` equivalent for this image, where possible

        If an ICC profile is attached to the image, we still attempt to resolve a Pillow
        mode.
        """

        m = ''
        if self.indexed:
            m = 'P'
        elif self.bits_per_component == 1:
            m = '1'
        elif self.bits_per_component == 8:
            if self.colorspace == '/DeviceRGB':
                m = 'RGB'
            elif self.colorspace == '/DeviceGray':
                m = 'L'
            elif self.colorspace == '/DeviceCMYK':
                m = 'CMYK'
            elif self.colorspace == '/ICCBased':
                try:
                    icc_profile = self._colorspaces[1]
                    icc_profile_nchannels = int(icc_profile['/N'])
                    if icc_profile_nchannels == 1:
                        m = 'L'
                    elif icc_profile_nchannels == 3:
                        m = 'RGB'
                    elif icc_profile_nchannels == 4:
                        m = 'CMYK'
                except (ValueError, TypeError):
                    pass
        if m == '':
            raise NotImplementedError("Not sure how to handle PDF image of this type")
        return m

    @property
    def filter_decodeparms(self):
        """PDF has a lot of optional data structures concerning /Filter and
        /DecodeParms. /Filter can be absent or a name or an array, /DecodeParms
        can be absent or a dictionary (if /Filter is a name) or an array (if
        /Filter is an array). When both are arrays the lengths match.

        Normalize this into:
        [(/FilterName, {/DecodeParmName: Value, ...}), ...]

        The order of /Filter matters as indicates the encoding/decoding sequence.
        """
        return list(zip_longest(self.filters, self.decode_parms, fillvalue={}))

    @property
    def palette(self):
        """Retrieves the color palette for this image

        Returns:
            tuple (base_colorspace: str, palette: bytes)
        """

        if not self.indexed:
            return None
        _idx, base, hival, lookup = None, None, None, None
        try:
            _idx, base, hival, lookup = self._colorspaces
        except ValueError as e:
            raise ValueError('Not sure how to interpret this palette') from e
        base = str(base)
        hival = int(hival)
        lookup = bytes(lookup)
        if not base in self.SIMPLE_COLORSPACES:
            raise NotImplementedError("not sure how to interpret this palette")
        if base == '/DeviceRGB':
            base = 'RGB'
        elif base == '/DeviceGray':
            base = 'L'
        return base, lookup

    @abstractmethod
    def as_pil_image(self):
        pass

    @staticmethod
    def _unstack_compression(buffer, filters):
        """Remove stacked compression where it appears.

        Stacked compression means when an image is set to:
            ``[/FlateDecode /DCTDecode]``
        for example.

        Only Flate can be stripped off the front currently.

        Args:
            buffer (pikepdf._qpdf.Buffer): the compressed image data
            filters (list of str): all files on the data
        """
        data = memoryview(buffer)
        while len(filters) > 1 and filters[0] == '/FlateDecode':
            try:
                data = decompress(data)
            except ZlibError as e:
                raise UnsupportedImageTypeError() from e
            filters = filters[1:]
        return data, filters


class PdfImage(PdfImageBase):
    """Support class to provide a consistent API for manipulating PDF images

    The data structure for images inside PDFs is irregular and flexible,
    making it difficult to work with without introducing errors for less
    typical cases. This class addresses these difficulties by providing a
    regular, Pythonic API similar in spirit (and convertible to) the Python
    Pillow imaging library.
    """

    def __new__(cls, obj):
        instance = super().__new__(cls)
        instance.__init__(obj)
        if '/JPXDecode' in instance.filters:
            instance = super().__new__(PdfJpxImage)
            instance.__init__(obj)
        return instance

    def __init__(self, obj):
        """Construct a PDF image from a Image XObject inside a PDF

        ``pim = PdfImage(page.Resources.XObject['/ImageNN'])``

        Args:
            obj (pikepdf.Object): an Image XObject

        """
        if isinstance(obj, Stream) and obj.stream_dict.get("/Subtype") != "/Image":
            raise TypeError("can't construct PdfImage from non-image")
        self.obj = obj
        self._icc = None

    @classmethod
    def _from_pil_image(cls, *, pdf, page, name, image):  # pragma: no cover
        """Insert a PIL image into a PDF (rudimentary)

        Args:
            pdf (pikepdf.Pdf): the PDF to attach the image to
            page (pikepdf.Object): the page to attach the image to
            name (str or pikepdf.Name): the name to set the image
            image (PIL.Image.Image): the image to insert
        """

        data = image.tobytes()

        imstream = Stream(pdf, data)
        imstream.Type = Name('/XObject')
        imstream.Subtype = Name('/Image')
        if image.mode == 'RGB':
            imstream.ColorSpace = Name('/DeviceRGB')
        elif image.mode in ('1', 'L'):
            imstream.ColorSpace = Name('/DeviceGray')
        imstream.BitsPerComponent = 1 if image.mode == '1' else 8
        imstream.Width = image.width
        imstream.Height = image.height

        page.Resources.XObject[name] = imstream

        return cls(imstream)

    def _metadata(self, name, type_, default):
        return metadata_from_obj(self.obj, name, type_, default)

    @property
    def is_inline(self):
        """``False`` for image XObject"""
        return False

    @property
    def icc(self):
        """If an ICC profile is attached, return a Pillow object that describe it.

        Most of the information may be found in ``icc.profile``.

        Returns:
            PIL.ImageCms.ImageCmsProfile
        """
        from PIL import ImageCms

        if self.colorspace != '/ICCBased':
            return None
        if not self._icc:
            iccstream = self._colorspaces[1]
            iccbuffer = iccstream.get_stream_buffer()
            iccbytesio = BytesIO(iccbuffer)
            self._icc = ImageCms.ImageCmsProfile(iccbytesio)
        return self._icc

    def _extract_direct(self, *, stream):
        """Attempt to extract the image directly to a usable image file

        If there is no way to extract the image without decompressing or
        transcoding then raise an exception. The type and format of image
        generated will vary.

        Args:
            stream: Writable stream to write data to
        """

        def normal_dct_rgb():
            # Normal DCTDecode RGB images have the default value of
            # /ColorTransform 1 and are actually in YUV. Such a file can be
            # saved as a standard JPEG. RGB JPEGs without YUV conversion can't
            # be saved as JPEGs, and are probably bugs. Some software in the
            # wild actually produces RGB JPEGs in PDFs (probably a bug).
            DEFAULT_CT_RGB = 1
            ct = self.filter_decodeparms[0][1].get('/ColorTransform', DEFAULT_CT_RGB)
            return self.mode == 'RGB' and ct == DEFAULT_CT_RGB

        def normal_dct_cmyk():
            # Normal DCTDecode CMYKs have /ColorTransform 0 and can be saved.
            # There is a YUVK colorspace but CMYK JPEGs don't generally use it
            DEFAULT_CT_CMYK = 0
            ct = self.filter_decodeparms[0][1].get('/ColorTransform', DEFAULT_CT_CMYK)
            return self.mode == 'CMYK' and ct == DEFAULT_CT_CMYK

        data, filters = self._unstack_compression(
            self.obj.get_raw_stream_buffer(), self.filters
        )

        if filters == ['/CCITTFaxDecode']:
            if self.colorspace == '/ICCBased':
                raise UnsupportedImageTypeError("Cannot direct-extract CCITT + ICC")
            stream.write(self._generate_ccitt_header(data))
            stream.write(data)
            return '.tif'
        elif filters == ['/DCTDecode'] and (
            self.mode == 'L' or normal_dct_rgb() or normal_dct_cmyk()
        ):
            stream.write(data)
            return '.jpg'

        raise UnsupportedImageTypeError()

    def _extract_transcoded(self):
        from PIL import Image

        im = None
        if self.mode == 'RGB' and self.bits_per_component == 8:
            # No point in accessing the buffer here, size qpdf decodes to 3-byte
            # RGB and Pillow needs RGBX for raw access
            data = self.read_bytes()
            im = Image.frombytes('RGB', self.size, data)
        elif self.mode in ('L', 'P') and self.bits_per_component == 8:
            buffer = self.get_stream_buffer()
            stride = 0  # tell Pillow to calculate stride from line width
            ystep = 1  # image is top to bottom in memory
            im = Image.frombuffer('L', self.size, buffer, "raw", 'L', stride, ystep)
            if self.mode == 'P':
                base_mode, palette = self.palette
                if base_mode in ('RGB', 'L'):
                    im.putpalette(palette, rawmode=base_mode)
                else:
                    raise NotImplementedError('palette with ' + base_mode)
        elif self.mode == '1' and self.bits_per_component == 1:
            data = self.read_bytes()
            im = Image.frombytes('1', self.size, data)

        elif self.mode == 'P' and self.bits_per_component == 1:
            data = self.read_bytes()
            im = Image.frombytes('1', self.size, data)

            base_mode, palette = self.palette
            if not (palette == b'\x00\x00\x00\xff\xff\xff' or palette == b'\x00\xff'):
                raise NotImplementedError('monochrome image with nontrivial palette')

        if self.colorspace == '/ICCBased':
            im.info['icc_profile'] = self.icc.tobytes()

        return im

    def _extract_to_stream(self, *, stream):
        """Attempt to extract the image directly to a usable image file

        If possible, the compressed data is extracted and inserted into
        a compressed image file format without transcoding the compressed
        content. If this is not possible, the data will be decompressed
        and extracted to an appropriate format.

        Because it is not known until attempted what image format will be
        extracted, users should not assume what format they are getting back.
        When saving the image to a file, use a temporary filename, and then
        rename the file to its final name based on the returned file extension.

        Args:
            stream: Writable stream to write data to

        Returns:
            str: The file format extension
        """

        try:
            return self._extract_direct(stream=stream)
        except UnsupportedImageTypeError:
            pass

        im = self._extract_transcoded()
        if im:
            im.save(stream, format='png')
            return '.png'

        raise UnsupportedImageTypeError(repr(self))

    def extract_to(self, *, stream=None, fileprefix=''):
        """Attempt to extract the image directly to a usable image file

        If possible, the compressed data is extracted and inserted into
        a compressed image file format without transcoding the compressed
        content. If this is not possible, the data will be decompressed
        and extracted to an appropriate format.

        Because it is not known until attempted what image format will be
        extracted, users should not assume what format they are getting back.
        When saving the image to a file, use a temporary filename, and then
        rename the file to its final name based on the returned file extension.

        Examples:

            >>> im.extract_to(stream=bytes_io)
            '.png'

            >>> im.extract_to(fileprefix='/tmp/image00')
            '/tmp/image00.jpg'

        Args:
            stream: Writable stream to write data to.
            fileprefix (str or Path): The path to write the extracted image to,
                without the file extension.

        Returns:
            If *fileprefix* was provided, then the fileprefix with the
            appropriate extension. If no *fileprefix*, then an extension
            indicating the file type.

        Return type:
            str
        """

        if bool(stream) == bool(fileprefix):
            raise ValueError("Cannot set both stream and fileprefix")
        if stream:
            return self._extract_to_stream(stream=stream)

        bio = BytesIO()
        extension = self._extract_to_stream(stream=bio)
        bio.seek(0)
        filepath = Path(str(Path(fileprefix)) + extension)
        with filepath.open('wb') as target:
            copyfileobj(bio, target)
        return str(filepath)

    def read_bytes(self):
        """Decompress this image and return it as unencoded bytes"""
        return self.obj.read_bytes()

    def get_stream_buffer(self):
        """Access this image with the buffer protocol"""
        return self.obj.get_stream_buffer()

    def as_pil_image(self):
        """Extract the image as a Pillow Image, using decompression as necessary

        Returns:
            PIL.Image.Image
        """
        from PIL import Image

        try:
            bio = BytesIO()
            self._extract_direct(stream=bio)
            bio.seek(0)
            return Image.open(bio)
        except UnsupportedImageTypeError:
            pass

        im = self._extract_transcoded()
        if not im:
            raise UnsupportedImageTypeError(repr(self))

        return im

    def _generate_ccitt_header(self, data):
        """Construct a CCITT G3 or G4 header from the PDF metadata"""
        # https://stackoverflow.com/questions/2641770/
        # https://www.itu.int/itudoc/itu-t/com16/tiff-fx/docs/tiff6.pdf

        if not self.decode_parms:
            raise ValueError("/CCITTFaxDecode without /DecodeParms")

        if self.decode_parms[0].get("/K", 1) < 0:
            ccitt_group = 4  # Pure two-dimensional encoding (Group 4)
        else:
            ccitt_group = 3
        black_is_one = self.decode_parms[0].get("/BlackIs1", False)
        white_is_zero = 1 if black_is_one else 0

        img_size = len(data)
        tiff_header_struct = '<' + '2s' + 'H' + 'L' + 'H' + 'HHLL' * 8 + 'L'
        # fmt: off
        tiff_header = struct.pack(
            tiff_header_struct,
            b'II',  # Byte order indication: Little endian
            42,  # Version number (always 42)
            8,  # Offset to first IFD
            8,  # Number of tags in IFD
            256, 4, 1, self.width,  # ImageWidth, LONG, 1, width
            257, 4, 1, self.height,  # ImageLength, LONG, 1, length
            258, 3, 1, 1,  # BitsPerSample, SHORT, 1, 1
            259, 3, 1, ccitt_group,  # Compression, SHORT, 1, 4 = CCITT Group 4 fax encoding
            262, 3, 1, int(white_is_zero),  # Thresholding, SHORT, 1, 0 = WhiteIsZero
            273, 4, 1, struct.calcsize(tiff_header_struct),  # StripOffsets, LONG, 1, length of header
            278, 4, 1, self.height,
            279, 4, 1, img_size,  # StripByteCounts, LONG, 1, size of image
            0  # last IFD
        )
        # fmt: on
        return tiff_header

    def show(self):
        """Show the image however PIL wants to"""
        self.as_pil_image().show()

    def __repr__(self):
        return '<pikepdf.PdfImage image mode={} size={}x{} at {}>'.format(
            self.mode, self.width, self.height, hex(id(self))
        )

    def _repr_png_(self):
        """Display hook for IPython/Jupyter"""
        b = BytesIO()
        im = self.as_pil_image()
        im.save(b, 'PNG')
        return b.getvalue()


class PdfJpxImage(PdfImage):
    def __init__(self, obj):
        super().__init__(obj)
        self.pil = self.as_pil_image()

    def _extract_direct(self, *, stream):
        data, filters = self._unstack_compression(
            self.obj.get_raw_stream_buffer(), self.filters
        )
        if filters != ['/JPXDecode']:
            raise UnsupportedImageTypeError(self.filters)
        stream.write(data)
        return '.jp2'

    @property
    def _colorspaces(self):
        # (PDF 1.7 Table 89) If ColorSpace is present, any colour space
        # specifications in the JPEG2000 data shall be ignored.
        super_colorspaces = super()._colorspaces
        if super_colorspaces:
            return super_colorspaces
        if self.pil.mode == 'L':
            return ['/DeviceGray']
        elif self.pil.mode == 'RGB':
            return ['/DeviceRGB']
        raise NotImplementedError('Complex JP2 colorspace')

    @property
    def _bpc(self):
        # (PDF 1.7 Table 89) If the image stream uses the JPXDecode filter, this
        # entry is optional and shall be ignored if present. The bit depth is
        # determined by the conforming reader in the process of decoding the
        # JPEG2000 image.
        return 8

    @property
    def indexed(self):
        # Nothing in the spec precludes an Indexed JPXDecode image, except for
        # the fact that doing so is madness. Let's assume it no one is that
        # insane.
        return False

    def __repr__(self):
        return '<pikepdf.PdfJpxImage JPEG2000 image mode={} size={}x{} at {}>'.format(
            self.mode, self.width, self.height, hex(id(self))
        )


class PdfInlineImage(PdfImageBase):
    """Support class for PDF inline images"""

    # Inline images can contain abbreviations that we write automatically
    ABBREVS = {
        b'/W': b'/Width',
        b'/H': b'/Height',
        b'/BPC': b'/BitsPerComponent',
        b'/IM': b'/ImageMask',
        b'/CS': b'/ColorSpace',
        b'/F': b'/Filter',
        b'/DP': b'/DecodeParms',
        b'/G': b'/DeviceGray',
        b'/RGB': b'/DeviceRGB',
        b'/CMYK': b'/DeviceCMYK',
        b'/I': b'/Indexed',
        b'/AHx': b'/ASCIIHexDecode',
        b'/A85': b'/ASCII85Decode',
        b'/LZW': b'/LZWDecode',
        b'/RL': b'/RunLengthDecode',
        b'/CCF': b'/CCITTFaxDecode',
        b'/DCT': b'/DCTDecode',
    }

    def __init__(self, *, image_data, image_object: tuple):
        """
        Args:
            image_data: data stream for image, extracted from content stream
            image_object: the metadata for image, also from content stream
        """

        # Convert the sequence of pikepdf.Object from the content stream into
        # a dictionary object by unparsing it (to bytes), eliminating inline
        # image abbreviations, and constructing a bytes string equivalent to
        # what an image XObject would look like. Then retrieve data from there

        self._data = image_data
        self._image_object = image_object

        reparse = b' '.join(self._unparse_obj(obj) for obj in image_object)
        try:
            reparsed_obj = Object.parse(b'<< ' + reparse + b' >>')
        except PdfError as e:
            raise PdfError("parsing inline " + reparse.decode('unicode_escape')) from e
        self.obj = reparsed_obj
        self.pil = None

    @classmethod
    def _unparse_obj(cls, obj):
        if isinstance(obj, Object):
            if isinstance(obj, Name):
                name = obj.unparse(resolved=True)
                assert isinstance(name, bytes)
                return cls.ABBREVS.get(name, name)
            else:
                return obj.unparse(resolved=True)
        elif isinstance(obj, bool):
            return b'true' if obj else b'false'  # Lower case for PDF spec
        elif isinstance(obj, (int, Decimal, float)):
            return str(obj).encode('ascii')
        else:
            raise NotImplementedError(repr(obj))

    def _metadata(self, name, type_, default):
        return metadata_from_obj(self.obj, name, type_, default)

    def unparse(self):
        tokens = []
        tokens.append(b'BI')
        metadata = []
        for metadata_obj in self._image_object:
            unparsed = self._unparse_obj(metadata_obj)
            assert isinstance(unparsed, bytes)
            metadata.append(unparsed)
        tokens.append(b' '.join(metadata))
        tokens.append(b'ID')
        tokens.append(self._data._inline_image_raw_bytes())
        tokens.append(b'EI')
        return b'\n'.join(tokens)

    @property
    def is_inline(self):
        return True

    @property
    def icc(self):
        raise ValueError("Inline images may not have ICC profiles")

    def __repr__(self):
        mode = '?'
        try:
            mode = self.mode
        except Exception:
            pass
        return '<pikepdf.PdfInlineImage image mode={} size={}x{} at {}>'.format(
            mode, self.width, self.height, hex(id(self))
        )

    def as_pil_image(self):
        if self.pil:
            return self.pil

        raise NotImplementedError('not yet')

    def extract_to(
        self, *, stream=None, fileprefix=''
    ):  # pylint: disable=unused-argument
        raise UnsupportedImageTypeError("inline images don't support extract")

    def read_bytes(self):
        raise NotImplementedError("qpdf returns compressed")
        # return self._data._inline_image_bytes()

    def get_stream_buffer(self):
        raise NotImplementedError("qpdf returns compressed")
        # return memoryview(self._data.inline_image_bytes())
