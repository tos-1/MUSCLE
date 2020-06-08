# MUSCLE
MUltiscale Spherical ColLapse Evolution code. It also performs Zel'dovich, 2lpt, alpt.
MUSCLE is an algorithm developed by Mark Neyrinck, whose original implementation can be found at http://skysrv.pha.jhu.edu/~neyrinck/muscle.html
We updated his code to run in python 3.6.9, alongside some minor modifications.

The main dependencies are numpy, scipy and pyfftw (https://pypi.org/project/pyFFTW/)

The cosmological module generates the linear power spectrum, which is produced with Eisentein and Hu fitting functions by default. Alternatively, one can use CLASS once installed (https://lesgourg.github.io/class_public/class.html version 2.9.3), albeit it is a bit slower.

See the quickstart.ipynb notebook for an example.

If you find this code useful, please cite https://ui.adsabs.harvard.edu/abs/2016MNRAS.455L..11N/abstract and .........
