import numpy as N
import cosmo
import pyfftw
import gadgetutils
import os
import warnings
import multiprocessing


class muscle(object):
    '''
    Inputs::
      cosmo: whether to use Eisenstein & Hu linear power spectrum ('ehu') or CLASS ('cls')
      h: normalized hubble rate
      omega_b: physical density of baryons
      Omega_cdm: cosmological cdm density
      ns: spectral index
      sigma8: power spectrum normalization
      z_pk: redshift of initial conditions
      redshift: redshift at which the output is computed
      ng: number of particles per side
      box: box size in Mpc/h
      sigmaalpt: scale of the interpolating kernel in alpt, in Mpc/h
      scheme: scheme among which to choose the evolution. The options are
        -zeld
        -2lpt
        -sc
        -muscle
      smallscheme: selecting this activates alpt. It works only with sc and muscle, while 2lpt on large scales is automatically set
      threads: number of threads used by pyfftw
      extra: initial string for the output folder and files
      seed: seed of the random number generator of initial conditions
      exact_pk: boolean to fix the fourier amplitudes of the initial density
      makeic: write the parameter file and the binaries for Gadget2. If z_pk!=redshift an error is raised. It works only with 2lpt
      pos: boolean to return the array of pos, otherwise positions are written on a binary in Gadget2 format
      saveto: string of the name of the folder where to store the binaries
    '''

    def __init__(
            self,
            cosmology='ehu',
            h=0.7,
            omega_b=0.0225,
            Omega_cdm=0.25,
            ns=0.96,
            sigma8=0.8,
            z_pk=50.,
            redshift=0.,
            ng=64,
            boxsize=64,
            sigmaalpt=4.,
            scheme='zeld',
            smallscheme=None,
            makeic=False,
            return_pos=True,
            threads=1,
            extra_info='',
            seed=1,
            exact_pk=True,
            saveto='sims'):

        self.ng = int(ng)
        self.thirdim = self.ng // 2 + 1
        self.boxsize = float(boxsize)
        self.cellsize = boxsize / float(ng)
        self.h = h
        self.redshift = redshift
        self.z_pk = z_pk
        self.saveto = saveto
        self.sigmaalpt = sigmaalpt
        self.ns = ns
        self.scheme = scheme
        self.smallscheme = smallscheme
        self.return_pos = return_pos
        self.extra_info = extra_info
        self.seed = seed
        self.exact_pk = exact_pk

        self.makeic = makeic
        if self.makeic:
            if not z_pk == redshift:
                raise ValueError(
                    "for initial conditions you need z_pk=redshift")

        # for fftw
        self.threads = threads
        cpus = multiprocessing.cpu_count()
        if not cpus >= threads:
            raise ValueError(
                "requested a number of threads > than available cpus")

        # store the kgrid
        self.kx, self.ky, self.kz, self.k = self.getkgrid()
        self.shc = N.shape(self.kx)
        self.shr = (self.shc[0], self.shc[1], (self.shc[2] - 1) * 2)

        # cosmology
        if cosmology == 'cls':
            try:
                from classy import Class
                self.C = cosmo.PSClass(h, omega_b, Omega_cdm, ns, sigma8)
            except ImportError:
                print('class is not installed, using ehu')
                self.C = cosmo.EisHu(h, omega_b, Omega_cdm, ns, sigma8)

        elif cosmology == 'ehu':
            self.C = cosmo.EisHu(h, omega_b, Omega_cdm, ns, sigma8)

        else:
            raise ValueError("select the cosmology correctly")

        self.D_i = self.C.d1(z_pk)
        self.D_f = self.C.d1(redshift)
        self.growth = self.D_f / self.D_i

        # growth factors, from Bouchet95
        self.f1 = self.C.Om0z(redshift)**(5. / 9.)
        self.f2 = 2. * self.C.Om0z(redshift)**(6. / 11.)

    def generate(self):
        ''' Main function '''

        # generate primordial density field
        dk = self.dk()

        # returns the displacement fields
        disp_field, vel = self.disp_field(dk)

        # get eulerian positions on the grid
        pos = self.get_pos(disp_field)

        # create the folders where binaries are stored
        if (self.makeic) or (self.return_pos == False):
            path, fileroot = gadgetutils.writedir(self.saveto,self.sigmaalpt, self.extra_info, scheme=self.scheme, smallscheme=self.smallscheme, redshift=self.redshift,
                                                  boxsize=self.boxsize, ngrid=self.ng, hubble=self.C.h, Omega0=self.C.Omega_0, makeic=self.makeic)

        if ((self.makeic) and (self.scheme == '2lpt') and (path is not None)):
            # write the param file for Gadget2
            gadgetutils.writeparam(
                path_sims=path,
                fileroot=fileroot,
                scheme=self.scheme,
                redshift=self.redshift,
                boxsize=self.boxsize,
                ngrid=self.ng,
                hubble=self.C.h,
                ombh2=self.C.omega_b,
                Omega0=self.C.Omega_0)
            print('written gadget param file')

        if (self.return_pos == False):
            if not self.makeic:
                vel = N.zeros_like(pos)

            gadgetutils.writegadget(
                pos,
                vel,
                self.redshift,
                self.boxsize,
                self.C.Omega_0,
                1. - self.C.Omega_0,
                self.C.h,
                path,
                fileroot,
                id=None)
            print('written binaries in', path + fileroot + '.dat')
            return 0
        else:
            return pos

    def get_pos(self, disp):
        '''
        From displacement field, get the Eulerian position with respect to an initial uniform grid
        '''
        xp, yp, zp = disp

        # setup particles on a uniform grid
        sh = xp.shape
        a, b, c = N.mgrid[0:sh[0], 0:sh[1], 0:sh[2]].astype(N.float32)

        a = self.cellsize * a
        b = self.cellsize * b
        c = self.cellsize * c

        a += xp
        b += yp
        c += zp

        # periodic boundary conditions PBC
        a = a % self.boxsize
        b = b % self.boxsize
        c = c % self.boxsize

        return a, b, c

    def invdiv(self, psi_k):
        ''' Returns the displacement field given the divergence field '''

        # initialize the fft of gradient of the displacement potential phi
        phixc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phiyc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phizc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phixr = pyfftw.empty_aligned(self.shr, dtype='float32')
        phiyr = pyfftw.empty_aligned(self.shr, dtype='float32')
        phizr = pyfftw.empty_aligned(self.shr, dtype='float32')
        ifftx_obj = pyfftw.FFTW(
            phixc, phixr, direction='FFTW_BACKWARD', axes=(
                0, 1, 2,), threads=self.threads)
        iffty_obj = pyfftw.FFTW(
            phiyc, phiyr, direction='FFTW_BACKWARD', axes=(
                0, 1, 2,), threads=self.threads)
        ifftz_obj = pyfftw.FFTW(
            phizc, phizr, direction='FFTW_BACKWARD', axes=(
                0, 1, 2,), threads=self.threads)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            #G = -1/self.k**2.
            G = self.cellsize**2. / 2. / (N.cos(self.kx * self.cellsize) + N.cos(
                self.ky * self.cellsize) + N.cos(self.kz * self.cellsize) - 3.)
            G.flat[0] = 0

        phixc = 1j * self.kx * G * psi_k
        phiyc = 1j * self.ky * G * psi_k
        phizc = 1j * self.kz * G * psi_k

        if N.isnan(phixc).any():
            print('something went wrong in Poisson eq')
            assert 0

        # diplacement field
        phixr = ifftx_obj(phixc)
        phiyr = iffty_obj(phiyc)
        phizr = ifftz_obj(phizc)

        return phixr, phiyr, phizr

    def disp_field(self, dk):
        ''' It returns the displacement field according to the scheme you chose '''

        vel = 0

        if self.smallscheme is None:

            if self.scheme == 'zeld':
                print("using Zel'dovich approximation")
                psi_k = -self.growth * dk
                disp_field = self.invdiv(psi_k)

                if self.makeic:
                    vel_factor = self.C.Om0z(
                        self.redshift)**(5. / 9.) * self.C.E(self.redshift) * 100. / (1. + self.redshift)
                    vel = tuple([vel_factor * x for x in disp_field[0::]])

            elif self.scheme == 'sc':
                print("using spherical collapse")
                psi_sc = pyfftw.empty_aligned(self.shr, dtype='float32')
                psi_sc_k = pyfftw.empty_aligned(self.shc, dtype='complex64')
                fft_sc = pyfftw.FFTW(
                    psi_sc,
                    psi_sc_k,
                    direction='FFTW_FORWARD',
                    axes=[
                        0,
                        1,
                        2])
                psi_sc = self.sc(dk)
                psi_sc_k = fft_sc(psi_sc)
                disp_field = self.invdiv(psi_sc_k)

            elif self.scheme == 'muscle':
                print("using muscle")
                psi = pyfftw.empty_aligned(self.shr, dtype='float32')
                psi_msc_k = pyfftw.empty_aligned(self.shc, dtype='complex64')
                fft_msc = pyfftw.FFTW(
                    psi,
                    psi_msc_k,
                    direction='FFTW_FORWARD',
                    axes=[
                        0,
                        1,
                        2])
                psi = self.muscle(dk)
                psi_msc_k = fft_msc(psi)
                disp_field = self.invdiv(psi_msc_k)

            elif self.scheme == '2lpt':
                print("using 2lpt")
                psi_2lpt = pyfftw.empty_aligned(self.shr, dtype='float32')
                psi_2lpt_k = pyfftw.empty_aligned(self.shc, dtype='complex64')
                fft_2lpt = pyfftw.FFTW(
                    psi_2lpt,
                    psi_2lpt_k,
                    direction='FFTW_FORWARD',
                    axes=[
                        0,
                        1,
                        2])
                psi_2lpt = self.twolpt(dk)
                psi_2lpt_k = fft_2lpt(psi_2lpt)
                psi_za_k = -self.growth * dk

                if not self.makeic:
                    disp_field = self.invdiv(psi_za_k + psi_2lpt_k)

                else:
                    disp_field1 = self.invdiv(psi_za_k)
                    disp_field2 = self.invdiv(psi_2lpt_k)
                    vel1 = self.C.Om0z(
                        self.redshift)**(5. / 9.) * self.C.E(self.redshift) * 100. / (1. + self.redshift)
                    vel2 = 2. * self.C.Om0z(self.redshift)**(6. / 11.) * \
                        self.C.E(self.redshift) * 100. / (1. + self.redshift)
                    vel1 = tuple([vel1 * x for x in disp_field1[0::]])
                    vel2 = tuple([vel2 * x for x in disp_field2[0::]])
                    vel = [sum(x) for x in zip(vel1, vel2)]
                    disp_field = [sum(x)
                                  for x in zip(disp_field1, disp_field2)]

            else:
                print('you did not correctly specify the gravity solver')
                assert 0

        else:  # ALPT case
            psi_k, psi2_k = self.alpt(dk)
            disp_field1 = self.invdiv(psi_k)
            disp_field2 = self.invdiv(psi2_k)
            disp_field = [sum(x) for x in zip(disp_field1, disp_field2)]

        return disp_field, vel

    def dk(self):
        ''' Makes a primordial gaussian density field in Fourier space '''

        r = N.random.RandomState(self.seed)
        sh = N.prod(self.shc)

        phase = r.uniform(0, 1, sh)

        if not self.exact_pk:
            amp = N.empty(sh, dtype=N.complex64)
            amp.real = r.normal(size=sh).astype(N.float32)
            amp.imag = r.normal(size=sh).astype(N.float32)
            amp /= N.sqrt(2.)
        else:
            amp = 1

        dk = amp * N.exp(2j * N.pi * r.uniform(0, 1, sh)).astype(N.complex64)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            pk = self.C.pk_lin(self.k.flatten(), self.z_pk).astype(N.complex64)

        dk *= N.sqrt(pk) / self.boxsize**1.5 * self.ng**3.
        dk.flat[0] = 0
        dk = N.reshape(dk, self.shc)

        # Hermitian symmetry: dk(-k) = conjugate(dk(k))
        dk[self.ng // 2 + 1:, 1:,
            0] = N.conj(N.fliplr(N.flipud(dk[1:self.ng // 2, 1:, 0])))
        dk[self.ng // 2 + 1:, 0, 0] = N.conj(dk[self.ng // 2 - 1:0:-1, 0, 0])
        dk[0, self.ng // 2 + 1:, 0] = N.conj(dk[0, self.ng // 2 - 1:0:-1, 0])
        dk[self.ng // 2, self.ng // 2 + 1:,
            0] = N.conj(dk[self.ng // 2, self.ng // 2 - 1:0:-1, 0])

        dk[self.ng // 2 + 1:, 1:, self.ng //
            2] = N.conj(N.fliplr(N.flipud(dk[1:self.ng // 2, 1:, self.ng // 2])))
        dk[self.ng // 2 + 1:, 0, self.ng //
            2] = N.conj(dk[self.ng // 2 - 1:0:-1, 0, self.ng // 2])
        dk[0, self.ng // 2 + 1:, self.ng //
            2] = N.conj(dk[0, self.ng // 2 - 1:0:-1, self.ng // 2])
        dk[self.ng // 2, self.ng // 2 + 1:, self.ng //
            2] = N.conj(dk[self.ng // 2, self.ng // 2 - 1:0:-1, self.ng // 2])

        return dk

    def getkgrid(self):
        '''
        It returns a grid of kx, ky ,kz and of modulus k
        '''
        kmin = 2 * N.pi / N.float(self.boxsize)
        sh = (self.ng, self.ng, self.thirdim)
        kx, ky, kz = N.mgrid[0:sh[0], 0:sh[1], 0:sh[2]].astype(N.float32)

        kx[N.where(kx > self.ng / 2)] -= self.ng
        ky[N.where(ky > self.ng / 2)] -= self.ng
        kz[N.where(kz > self.ng / 2)] -= self.ng

        kx *= kmin
        ky *= kmin
        kz *= kmin

        k = N.sqrt(kx**2 + ky**2 + kz**2)

        return kx, ky, kz, k

    def sc(self, dk):
        ''' spherical collapse '''

        # need the linear psi
        psi_za = pyfftw.empty_aligned(self.shr, dtype='float32')
        psi_za_k = pyfftw.empty_aligned(self.shc, dtype='complex64')
        ifft_za = pyfftw.FFTW(
            psi_za_k,
            psi_za,
            direction='FFTW_BACKWARD',
            axes=[
                0,
                1,
                2])
        psi_za_k = -dk * self.growth

        psi_za = ifft_za(psi_za_k)

        # collapse condition
        cc = 1. + psi_za * 2. / 3.

        wnc = N.where(cc > 0.)
        wc = N.where(cc <= 0.)
        psi_za[wnc] = 3. * (N.sqrt(1. + psi_za[wnc] * 2. / 3.) - 1.)
        psi_za[wc] = -3.

        # impose zero mean for the non collapsed regions
        psi_za[wnc] -= N.sum(psi_za.flatten()) / len(psi_za[wnc].flatten())

        return psi_za

    def muscle(self, dk):
        ''' MUltiscale Spherical colLapse Evolution '''

        psi_k = -dk * self.growth
        psi = N.fft.irfftn(psi_k)

        # number of possible iterations
        twofolds = int(N.log(self.ng) / N.log(2.))

        # collapse condition to consider
        cc = 1 + psi * 2. / 3.

        starter = 0
        for i in N.arange(starter, twofolds):
            sigma = 2**i
            sigma_k = 2. * N.pi / sigma
            Wk = N.exp(-(self.k / sigma_k)**2 / 2.)
            Wk.flat[0] = 1

            psi_k_R = Wk * psi_k
            psi_R = N.fft.irfftn(psi_k_R)

            cc_R = 1 + (2. / 3.) * psi_R
            cc_R_min = N.min(cc_R)

            # if we're so low-res that nothing's collapsing, no voids in clouds
            if cc_R_min > 0.:
                break

            # does it collapse at any scale?
            w = (N.where(cc_R <= 0.) or N.where(cc <= 0.))
            cc[w] = N.minimum(cc_R[w], cc[w])

        # where no collapse
        wnc = N.where(cc > 0.)
        wc = N.where(cc <= 0.)
        psi[wnc] = 3. * (N.sqrt(1 + (2. / 3.) * psi[wnc]) - 1.)
        psi[wc] = -3.
        psi[wnc] -= N.sum(psi.flatten()) / len(psi[wnc].flatten())

        return psi

    def twolpt(self, dk):
        ''' it returns the displacement potential at second order '''

        # initialize the fft of gradient of the displacement potential phi
        phixxc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phiyyc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phizzc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phixyc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phixzc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phiyzc = pyfftw.empty_aligned(self.shc, dtype='complex64')
        phixxr = pyfftw.empty_aligned(self.shr, dtype='float32')
        phiyyr = pyfftw.empty_aligned(self.shr, dtype='float32')
        phizzr = pyfftw.empty_aligned(self.shr, dtype='float32')
        phixyr = pyfftw.empty_aligned(self.shr, dtype='float32')
        phixzr = pyfftw.empty_aligned(self.shr, dtype='float32')
        phiyzr = pyfftw.empty_aligned(self.shr, dtype='float32')
        ifftxx_obj = pyfftw.FFTW(
            phixxc, phixxr, direction='FFTW_BACKWARD', axes=[
                0, 1, 2], threads=self.threads)
        ifftyy_obj = pyfftw.FFTW(
            phiyyc, phiyyr, direction='FFTW_BACKWARD', axes=[
                0, 1, 2], threads=self.threads)
        ifftzz_obj = pyfftw.FFTW(
            phizzc, phizzr, direction='FFTW_BACKWARD', axes=[
                0, 1, 2], threads=self.threads)
        ifftxy_obj = pyfftw.FFTW(
            phixyc, phixyr, direction='FFTW_BACKWARD', axes=[
                0, 1, 2], threads=self.threads)
        ifftxz_obj = pyfftw.FFTW(
            phixzc, phixzr, direction='FFTW_BACKWARD', axes=[
                0, 1, 2], threads=self.threads)
        ifftyz_obj = pyfftw.FFTW(
            phiyzc, phiyzr, direction='FFTW_BACKWARD', axes=[
                0, 1, 2], threads=self.threads)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            #G = -1/self.k**2.
            G = self.cellsize**2. / 2. / (N.cos(self.kx * self.cellsize) + N.cos(
                self.ky * self.cellsize) + N.cos(self.kz * self.cellsize) - 3.)

        G.flat[0] = 0

        phixxc = -self.kx * self.kx * G * dk
        phixyc = -self.kx * self.ky * G * dk
        phixzc = -self.kx * self.kz * G * dk
        phiyyc = -self.ky * self.ky * G * dk
        phiyzc = -self.ky * self.kz * G * dk
        phizzc = -self.kz * self.kz * G * dk

        # diplacement field
        phixxr = ifftxx_obj(phixxc)
        phixyr = ifftxy_obj(phixyc)
        phixzr = ifftxz_obj(phixzc)
        phiyyr = ifftyy_obj(phiyyc)
        phiyzr = ifftyz_obj(phiyzc)
        phizzr = ifftzz_obj(phizzc)

        # phi2
        phixxr = phixxr * phiyyr + phixxr * phizzr + phiyyr * phizzr \
            - phixyr * phixyr - phixzr * phixzr - phiyzr * phiyzr

        # account time evolution
        phixxr *= - self.growth**2. * 3. / 7.

        return phixxr

    def alpt(self, dk):
        """
        Interpolates between large- and small-scale displacement divergences.
        """
        # small scale overdensity determined by sc
        if self.smallscheme == 'sc':
            print('implementing alpt with sc')
            psi_k_small = N.fft.rfftn(self.sc(dk))

        elif self.smallscheme == 'muscle':
            print('implementing alpt with muscle')
            psi_k_small = N.fft.rfftn(self.muscle(dk))

        else:
            print('you did not choose correctly the small scale scheme')
            assert 0

        # large scale overdensity determined by 2lpt
        psi_k_alpt = [-self.growth * dk, N.fft.rfftn(self.twolpt(dk))]

        print('sigma of alpt: ', self.sigmaalpt)

        gaussian = N.exp(-(self.sigmaalpt * self.k)**2 / 2.)

        psi_k_alpt[0] = psi_k_small * \
            (1. - gaussian) + psi_k_alpt[0] * gaussian
        psi_k_alpt[1] = psi_k_alpt[1] * gaussian

        # get the fourier transformed displacement potentials
        return psi_k_alpt[0], psi_k_alpt[1]
