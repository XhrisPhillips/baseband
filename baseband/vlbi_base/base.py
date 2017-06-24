import warnings
import numpy as np
from astropy import units as u
from astropy.utils import lazyproperty


__all__ = ['u_sample', 'VLBIStreamBase', 'VLBIStreamReaderBase',
           'VLBIStreamWriterBase']

u_sample = u.def_unit('sample', doc='One sample from a data stream')


class VLBIFileBase(object):
    """VLBI file wrapper, used to add frame methods to a binary data file.

    The underlying file is stored in ``fh`` and all attributes that do not
    exist on the class itself are looked up on it.
    """
    def __init__(self, fh_raw):
        self.fh_raw = fh_raw

    def __getattr__(self, attr):
        """Try to get things on the current open file if it is not on self."""
        if not attr.startswith('_'):
            try:
                return getattr(self.fh_raw, attr)
            except AttributeError:
                pass
        return self.__getattribute__(attr)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        self.fh_raw.__exit__(exc_type, exc_val, exc_tb)

    def close(self):
        self.fh_raw.close()

    def __repr__(self):
        return "{0}(fh_raw={1})".format(self.__class__.__name__, self.fh_raw)


class VLBIStreamBase(VLBIFileBase):
    """VLBI file wrapper, allowing access as a stream of data."""

    _frame_class = None

    def __init__(self, fh_raw, header0, nchan, bps, complex_data, thread_ids,
                 samples_per_frame, frames_per_second=None,
                 sample_rate=None):
        super(VLBIStreamBase, self).__init__(fh_raw)
        self.header0 = header0
        self.nchan = nchan
        self.bps = bps
        self.complex_data = complex_data
        self.thread_ids = thread_ids
        self.nthread = nchan if thread_ids is None else len(thread_ids)
        self.samples_per_frame = samples_per_frame
        if frames_per_second is None:
            frames_per_second = sample_rate.to(u.Hz).value / samples_per_frame
            if frames_per_second % 1:  # pragma: no cover
                warnings.warn("Sampling rate {0} and samples per frame {1} "
                              "imply non-integer number of frames per "
                              "second".format(sample_rate, samples_per_frame))
            else:
                frames_per_second = int(frames_per_second)

        self.frames_per_second = frames_per_second
        self.offset = 0

    def _get_time(self, header):
        """Get time from a header."""
        # Subclasses can override this if information is needed beyond that
        # provided in the header.
        return header.time

    @lazyproperty
    def time0(self):
        """Start time."""
        return self._get_time(self.header0)

    def tell(self, unit=None):
        """Current offset in file.

        Parameters
        ----------
        unit : `~astropy.units.Unit` or str, optional
            Time unit the offset should be returned in.  By default, no unit
            is used, i.e., an integer enumerating samples is returned. For the
            special string 'time', the absolute time is calculated.

        Returns
        -------
        offset : int, `~astropy.units.Quantity`, or `~astropy.time.Time`
             Offset in current file (or time at current position)
        """
        if unit is None:
            return self.offset

        if unit == 'time':
            return self.time0 + self.tell(unit=u.s)

        return (self.offset * u_sample).to(unit, equivalencies=[(u.s, u.Unit(
            self.samples_per_frame * self.frames_per_second * u_sample))])

    def _frame_info(self):
        offset = (self.offset +
                  self.header0['frame_nr'] * self.samples_per_frame)
        full_frame_nr, extra = divmod(offset, self.samples_per_frame)
        dt, frame_nr = divmod(full_frame_nr, self.frames_per_second)
        return int(dt), int(frame_nr), extra

    def __enter__(self):
        super(VLBIStreamBase, self).__enter__()
        return self

    def __repr__(self):
        return ("<{s.__class__.__name__} name={s.name} offset={s.offset}\n"
                "    samples_per_frame={s.samples_per_frame}, nchan={s.nchan},"
                " frames_per_second={s.frames_per_second}, bps={s.bps},\n"
                "    {t}(start) time={s.time0.isot}>"
                .format(s=self, t=('thread_ids={0}, '.format(self.thread_ids)
                                   if self.thread_ids else '')))


class VLBIStreamReaderBase(VLBIStreamBase):

    def __init__(self, fh_raw, header0, nchan, bps, complex_data, thread_ids,
                 samples_per_frame, frames_per_second=None,
                 sample_rate=None):

        if frames_per_second is None and sample_rate is None:
            frames_per_second = self._get_frame_rate(fh_raw, type(header0))

        super(VLBIStreamReaderBase, self).__init__(
            fh_raw, header0, nchan, bps, complex_data, thread_ids,
            samples_per_frame, frames_per_second, sample_rate)

    @staticmethod
    def _get_frame_rate(fh, header_class):
        """Returns the number of frames in one second of data."""
        oldpos = fh.tell()
        header = header_class.fromfile(fh)
        frame_nr0 = header['frame_nr']
        sec0 = header.seconds
        while header['frame_nr'] == frame_nr0:
            fh.seek(header.payloadsize, 1)
            header = header_class.fromfile(fh)
        while header['frame_nr'] > 0:
            max_frame = header['frame_nr']
            fh.seek(header.payloadsize, 1)
            header = header_class.fromfile(fh)

        if header.seconds != sec0 + 1:  # pragma: no cover
            warnings.warn("Header time changed by more than 1 second?")

        fh.seek(oldpos)
        return max_frame + 1

    @lazyproperty
    def header1(self):
        """Last header of the file."""
        raw_offset = self.fh_raw.tell()
        self.fh_raw.seek(-self.header0.framesize, 2)
        header1 = self.find_header(template_header=self.header0,
                                   maximum=10*self.header0.framesize,
                                   forward=False)
        self.fh_raw.seek(raw_offset)
        if header1 is None:
            raise ValueError("Corrupt VLBI frame? No frame in last {0} bytes."
                             .format(10*self.header0.framesize))
        return header1

    @lazyproperty
    def time1(self):
        """Time of the sample just beyond the last one in the file."""
        return self._get_time(self.header1) + u.s / self.frames_per_second

    @property
    def size(self):
        """Number of samples in the file."""
        return int(round((self.time1 - self.time0).to(u.s).value *
                         self.frames_per_second * self.samples_per_frame))

    def seek(self, offset, whence=0):
        """Change stream position.

        This works like a normal seek, but the offset is in samples
        (or a relative or absolute time).

        Parameters
        ----------
        offset : int, `~astropy.units.Quantity`, or `~astropy.time.Time`
            Offset to move to.  Can be an (integer) number of samples,
            an offset in time units, or an absolute time.
        whence : int
            Like regular seek, the offset is taken to be from the start if
            ``whence=0`` (default), from the current position if ``1``,
            and from the end if ``2``.  Ignored if ``offset`` is a time.`
        """
        try:
            offset = offset.__index__()
        except:
            try:
                offset = offset - self.time0
            except:
                pass
            else:
                whence = 0

            offset = offset.to(u_sample, equivalencies=[(u.s, u.Unit(
                self.samples_per_frame * self.frames_per_second * u_sample))])
            offset = int(round(offset.value))

        if whence == 0:
            self.offset = offset
        elif whence == 1:
            self.offset += offset
        elif whence == 2:
            self.offset = self.size + offset
        else:
            raise ValueError("invalid 'whence'; should be 0, 1, or 2.")

        return self.offset


class VLBIStreamWriterBase(VLBIStreamBase):
    def close(self):
        extra = self.offset % self.samples_per_frame
        if extra != 0:
            warnings.warn("Closing with partial buffer remaining."
                          "Writing padded frame, marked as invalid.")
            self.write(np.zeros((self.samples_per_frame - extra,
                                 self.nthread, self.nchan)),
                       invalid_data=True)
            assert self.offset % self.samples_per_frame == 0
        return super(VLBIStreamWriterBase, self).close()
