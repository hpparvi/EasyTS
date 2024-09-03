#  EasyTS: fast, flexible, and easy exoplanet transmission spectroscopy in Python.
#  Copyright (C) 2024 Hannu Parviainen
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.

import codecs
import json
import pickle
from pathlib import Path
from typing import Optional, Callable, Any, Iterable

import pandas as pd
import seaborn as sb
import astropy.io.fits as pf

from astropy.table import Table
from celerite2 import GaussianProcess, terms
from matplotlib.pyplot import subplots, setp, figure, Figure, GridSpec, Axes
from numpy import (poly1d, polyfit, where, sqrt, clip, percentile, median, squeeze, floor, ndarray,
                   array, inf, newaxis, r_, arange, tile, log10, sort, argsort)
from numpy.random import normal, permutation
from pytransit.orbits import fold
from pytransit.param import ParameterSet
from pytransit.utils.de import DiffEvol
from scipy.stats import norm

from .ldtkld import LDTkLD
from .tsdata import TSData, TSDataSet
from .tslpf import TSLPF, clean_knots
from .wlpf import WhiteLPF


def load_model(fname, name: str | None = None):
    """Loads an EasyTS analysis from a FITS file.

    Parameters
    ----------
    fname : str
        The name of the savefile.

    name : str, optional
        The name of the new EasyTS model. If not provided, the original analysis name will be used.

    Returns
    -------
    a : EasyTS
        An EasyTS analysis.

    Raises
    ------
    IOError
        If there is an error while opening or reading the file.

    ValueError
        If the file format is invalid or does not match the expected format.
    """
    with pf.open(fname) as hdul:
        d = []
        for i in range(hdul[0].header['NDGROUPS']):
            d.append(TSData(hdul[f'TIME_{i}'].data.astype('d'), hdul[f'WAVELENGTH_{i}'].data.astype('d'),
                            hdul[f'FLUX_{i}'].data.astype('d'), hdul[f'FERR_{i}'].data.astype('d'),
                            name=hdul[f'FLUX_{i}'].header['NAME']))
        data = TSDataSet(d)

        if hdul[0].header['LDMODEL'] == 'ldtk':
            filters, teff, logg, metal, dataset = pickle.loads(codecs.decode(json.loads(hdul[0].header['LDTKLD']).encode(), "base64"))
            ldm = LDTkLD(filters, teff, logg, metal, dataset=dataset)
        else:
            ldm =  hdul[0].header['LDMODEL']

        a = EasyTS(name or hdul[0].header['NAME'], ldmodel=ldm, data=data)
        a.set_radius_ratio_knots(hdul['K_KNOTS'].data.astype('d'))
        a.set_limb_darkening_knots(hdul['LD_KNOTS'].data.astype('d'))
        a.transit_center = hdul[0].header['T0']
        a.transit_duration = hdul[0].header['T14']

        if a.transit_duration is not None:
            a.ootmask = abs(a.time - a.transit_center) > 0.502 * a.transit_duration

        priors = pickle.loads(codecs.decode(json.loads(hdul['PRIORS'].header['PRIORS']).encode(), "base64"))
        a._tsa.ps = ParameterSet([pickle.loads(p) for p in priors])
        a._tsa.ps.freeze()
        if 'DE' in hdul:
            a._tsa._de_population = Table(hdul['DE'].data).to_pandas().values
            a._tsa._de_imin = hdul['DE'].header['IMIN']
        if 'MCMC' in hdul:
            npop = hdul['MCMC'].header['NPOP']
            ndim = hdul['MCMC'].header['NDIM']
            a._tsa._mc_chains = Table(hdul['MCMC'].data).to_pandas().values.reshape([npop, -1, ndim])
        return a


class EasyTS:
    """A class providing tools for easy and fast exoplanet transit spectroscopy.

    EasyTS is a class providing the tools for easy and fast exoplanet transit spectroscopy. It provides methods
    for modelling spectroscopic transit light curves and inferring posterior densities for the model parameters.

    Attributes
    ----------
    name : str
        The name of the analysis.
    data : TSData
        The time-series data object.
    time : ndarray
        The array of time values.
    wavelength : ndarray
        The array of wavelength values.
    fluxes : ndarray
        The array of flux values.
    npb : int
        The number of wavelength bins.
    nthreads : int
        The number of threads to use for computation.
    de : None
        The Differential Evolution global optimizer.
    sampler : None
        The MCMC sampler.
    """

    def __init__(self, name: str, ldmodel, data: TSData | TSDataSet, nk: int = None, nldc: int = 10, nthreads: int = 1, tmpars=None,
                 noise_model: str = 'white'):
        """
        Parameters
        ----------
        name : str
            The name of the instance.
        ldmodel : object
            The model for the limb darkening.
        data : TSData
            The time-series data object.
        nk : int, optional
            The number of kernel samples.
        nbl : int, optional
            The number of bins for the light curve.
        nldc : int, optional
            The number of limb darkening coefficients.
        nthreads : int, optional
            The number of threads to use for computation.
        tmpars : object, optional
            Additional parameters.
        """
        data = TSDataSet([data]) if isinstance(data, TSData) else data
        self._tsa = TSLPF(name, ldmodel, data, nk=nk, nldc=nldc, nthreads=nthreads, tmpars=tmpars, noise_model=noise_model)
        self._wa = None
        self.nthreads = nthreads
        self.ootmask = None
        self.de: Optional[DiffEvol] = None
        self.sampler = None

        self.transit_center: Optional[float] = None
        self.transit_duration: Optional[float] = None

        self._tref = floor(self.time.min())
        self._extent = (self.time[0] - self._tref, self.time[-1] - self._tref, self.wavelength[0], self.wavelength[-1])

    def lnposterior(self, pvp):
        """Calculate the log posterior probability for a single parameter vector or an array of parameter vectors.

        Parameters
        ----------
        pvp : array_like
            The vector of parameter values or an array of parameter vectors with a shape [npv, np].

        Returns
        -------
        array_like
            The natural logarithm of the posterior probability.
        """
        return squeeze(self._tsa.lnposterior(pvp))

    def set_noise_model(self, noise_model: str) -> None:
        """Set the noise model for the analysis.

        Parameters
        ----------
        noise_model : str
            The noise model to be used. Must be one of the following: white, fixed_gp, free_gp.

        Raises
        ------
        ValueError
            If noise_model is not one of the specified options.
        """
        self._tsa.set_noise_model(noise_model)

    def set_data(self, data: TSData | TSDataSet) -> None:
        """Set the model data.

        Parameters
        ----------
        data : TSData
           The time-series data object.
        """
        data = TSDataSet([data]) if isinstance(data, TSData) else data
        self._tsa.set_data(data)

    def set_prior(self, parameter, prior, *nargs) -> None:
        """Set a prior on a model parameter.

        Parameters
        ----------
        parameter : str
            The name of the parameter to set prior for.

        prior : str or Object
            The prior distribution to set for the parameter. This can be "NP" for a normal prior, "UP" for a
            uniform prior, or an object with .logpdf(x) method.

        *nargs : tuple
            Additional arguments to be passed to the prior: (mean, std) for the normal prior and (min, max)
            for the uniform prior.
        """
        self._tsa.set_prior(parameter, prior, *nargs)

    def set_radius_ratio_prior(self, prior, *nargs) -> None:
        """Set an identical prior on all radius ratio (k) knots.

        Parameters
        ----------
        prior : float
            The prior for the radius ratios.
        *nargs : float
            Additional arguments for the prior.
        """
        for l in self._tsa.k_knots:
            self.set_prior(f'k_{l:08.5f}', prior, *nargs)

    def set_ldtk_prior(self, teff, logg, metal, dataset: str = 'visir-lowres', width: float = 50, uncertainty_multiplier: float = 10):
        """Set priors on the limb darkening parameters using LDTk.

        Sets priors on the limb darkening parameters based on theoretical stellar models using LDTk.

        Parameters
        ----------
        teff : float
            The effective temperature in Kelvin.

        logg : float
            The surface gravity in cm/s^2.

        metal : float
            The metallicity.

        dataset : str, optional
            The name of the dataset. Default is 'visir-lowres'.

        width : float, optional
            The passband width in nanometers. Default is 50.

        uncertainty_multiplier : float, optional
            The uncertainty multiplier to adjust the width of the prior. Default is 10.

        """
        self._tsa.set_ldtk_prior(teff, logg, metal, dataset, width, uncertainty_multiplier)

    def set_gp_hyperparameters(self, sigma: float, rho: float) -> None:
        """Set Gaussian Process (GP) hyperparameters assuming a Matern-3/2 kernel..

        Parameters
        ----------
        sigma : float
            The kernel amplitude parameter.

        rho : float
            The length scale parameter.
        """
        self._tsa.set_gp_hyperparameters(sigma, rho)

    def set_gp_kernel(self, kernel: terms.Term) -> None:
        """Set the Gaussian Process (GP) kernel.

        Parameters
        ----------
        kernel : terms.Term
            The kernel to set for the GP.
        """
        self._tsa.set_gp_kernel(kernel)

    @property
    def name(self) -> str:
        return self._tsa.name

    @name.setter
    def name(self, name: str):
        self._tsa.name = name

    @property
    def data(self) -> TSDataSet:
        return self._tsa.data

    @property
    def original_data(self) -> TSDataSet:
        return self._tsa._original_data

    @property
    def time(self) -> ndarray:
        return self._tsa.time

    @property
    def wavelength(self) -> ndarray:
        return self._tsa.wavelength

    @property
    def fluxes(self) -> ndarray:
        return self._tsa.flux

    @property
    def errors(self) -> ndarray:
        return self._tsa.ferr

    @property
    def k_knots(self) -> ndarray:
        """Get the radius ratio (k) knots."""
        return self._tsa.k_knots

    @property
    def ndim(self) -> int:
        """Get the number of free model parameters."""
        return self._tsa.ndim

    @property
    def nk(self) -> int:
        """Get the number of radius ratio (k) knots."""
        return self._tsa.nk

    @property
    def nldp(self) -> int:
        """Get the number of limb darkening knots."""
        return self._tsa.nldc

    @property
    def npb(self):
        """Get the number of passbands."""
        return self._tsa.npb

    @property
    def ldmodel(self):
        """Get the limb darkening model."""
        return self._tsa.ldmodel

    @property
    def gp(self) -> GaussianProcess:
        """Get the Gaussian Process (GP) model."""
        return self._tsa._gp

    @property
    def optimizer_population(self):
        """Get the current population of the optimizer."""
        return self._tsa._de_population

    @property
    def mcmc_chains(self):
        """Get the current emcee MCMC chains."""
        return self._tsa._mc_chains

    @property
    def posterior_samples(self):
        """Get the posterior samples from the MCMC sampler."""
        return pd.DataFrame(self._tsa._mc_chains.reshape([-1, self.ndim]), columns=self.ps.names)

    def add_radius_ratio_knots(self, knot_wavelengths) -> None:
        """Add radius ratio (k) knots.

        Parameters
        ----------
        knot_wavelengths : array-like
            List or array of knot wavelengths to be added.
        """
        self._tsa.add_k_knots(knot_wavelengths)

    def set_radius_ratio_knots(self, knot_wavelengths) -> None:
        """Set the radius ratio (k) knots.

        Parameters
        ----------
        knot_wavelengths : array-like
            List or array of knot wavelengths.
        """
        self._tsa.set_k_knots(knot_wavelengths)

    def add_limb_darkening_knots(self, knot_wavelengths) -> None:
        """Add limb darkening knots.

        Parameters
        ----------
        knot_wavelengths : array-like
            List or array of knot wavelengths to be added.
        """
        self._tsa.add_limb_darkening_knots(knot_wavelengths)

    def set_limb_darkening_knots(self, knot_wavelengths) -> None:
        """Set the limb darkening knots.

        Parameters
        ----------
        knot_wavelengths : array-like
            List or array of knot wavelengths.
        """
        self._tsa.set_ld_knots(knot_wavelengths)

    @property
    def ps(self) -> ParameterSet:
        """Get the model parameterization."""
        return self._tsa.ps

    def print_parameters(self):
        """Print the model parameterization."""
        self._tsa.print_parameters(1)

    def plot_setup(self, figsize=None, xscale=None, xticks=None) -> Figure:
        """Plot the model setup with limb darkening knots, radius ratio knots, and data binning.

        Parameters
        ----------
        figsize : tuple, optional
            The size of the figure. Default is (13, 4).

        Returns
        -------
        Figure
        """
        using_ldtk = isinstance(self._tsa.ldmodel, LDTkLD)

        if not using_ldtk:
            figsize = figsize or (13, 4)
            fig, axs = subplots(3, 1, figsize=figsize, sharex='all', sharey='all')
            axl, axk, axw = axs

            axl.vlines(self._tsa.ld_knots, 0.1, 0.5, ec='k')
            axl.text(0.01, 0.90, 'Limb darkening knots', va='top', transform=axl.transAxes)
        else:
            figsize = figsize or (13, 2*4/3)
            fig, axs = subplots(2, 1, figsize=figsize, sharex='all', sharey='all')
            axk, axw = axs
            axl = None

        axk.vlines(self._tsa.k_knots, 0.1, 0.5, ec='k')
        axk.text(0.01, 0.90, 'Radius ratio knots', va='top', transform=axk.transAxes)
        axw.vlines(self.wavelength, 0.1, 0.5, ec='k')
        axw.text(0.01, 0.90, 'Wavelength bins', va='top', transform=axw.transAxes)

        if not using_ldtk:
            sb.despine(ax=axl, top=False, bottom=True, right=False)
            sb.despine(ax=axk, top=True, bottom=True, right=False)
        else:
            sb.despine(ax=axk, top=False, bottom=True, right=False)

        sb.despine(ax=axw, top=True, bottom=False, right=False)
        setp(axs, xlim=(self.wavelength[0]-0.02, self.wavelength[-1]+0.02), yticks=[], ylim=(0, 0.9))
        setp(axw, xlabel=r'Wavelength [$\mu$m]')
        setp(axs[0].get_xticklines(), visible=False)
        setp(axs[0].get_xticklabels(), visible=False)
        setp(axs[1].get_xticklines(), visible=False)
        setp(axs[-1].get_xticklines(), visible=True)

        if xscale:
            setp(axs, xscale=xscale)
        if xticks is not None:
            [ax.set_xticks(xticks, labels=xticks) for ax in axs]
        fig.tight_layout()
        return fig

    def fit_white(self) -> None:
        """Fit a white light curve model and sets the out-of-transit mask."""
        self._wa = WhiteLPF(self._tsa)
        self._wa.optimize()
        pv = self._wa._local_minimization.x
        self.transit_center = self._wa.transit_center
        self.transit_duration = self._wa.transit_duration
        phase = fold(self.time, pv[1], pv[0])
        self.ootmask = abs(phase) > 0.502 * self.transit_duration

    def plot_white(self) -> Figure:
        """Plot the white light curve data with the best-fit model.

        Returns
        -------
        Figure
        """
        return self._wa.plot()

    def normalize_baseline(self, deg: int = 1) -> None:
        """Nortmalize the baseline flux for each spectroscopic light curve.

        Normalize the baseline flux using a low-order polynomial fitted to the out-of-transit
        data for each spectroscopic light curve.

        Parameters
        ----------
        deg : int
            The degree of the fitted polynomial. Should be 0 or 1. Higher degrees are not allowed
            because they could affect the transit depths.

        Returns
        -------
        None

        Raises
        ------
        ValueError
            If `deg` is greater than 1.

        Notes
        -----
        This method normalizes the baseline of the fluxes for each planet. It fits a polynomial of degree
        `deg` to the out-of-transit data points and divides the fluxes by the fitted polynomial evaluated
        at each time point.
        """
        if deg > 1:
            raise ValueError("The degree of the fitted polynomial ('deg') should be 0 or 1. Higher degrees are not allowed because they could affect the transit depths.")
        for ipb in range(self.npb):
            pl = poly1d(polyfit(self.time[self.ootmask], self.fluxes[ipb, self.ootmask], deg=deg))(self.time)
            self.fluxes[ipb, :] /= pl
            self.errors[ipb, :] /= pl
            for i, sl in enumerate(self.data.groups):
                self.data.data[i].fluxes[:, :] = self.fluxes[sl, :]
                self.data.data[i].errors[:, :] = self.errors[sl, :]

    def plot_baseline(self, axs: Optional[Iterable[Axes]] = None) -> Figure:
        """Plot the out-of-transit spectroscopic light curves before and after the normalization.

        Parameters
        ----------
        axs : Optional[Iterable[Axes]], optional
            Array of axes to plot on. If None, new axes will be created.

        Returns
        -------
        Figure
            The figure containing the subplots.
        """
        if axs is None:
            fig, axs = subplots(self.data.ngroups, 2, figsize=(13, 4), squeeze=False, constrained_layout=True)
        else:
            fig = axs[0,0].figure

        self.original_data.plot(ax=axs[:, 0], data=where(self.ootmask, self.original_data.fluxes, 1))
        self.data.plot(ax=axs[:, 1], data=where(self.ootmask, self.data.fluxes, 1))
        return fig

    def fit(self, niter: int = 200, npop: Optional[int] = None, pool: Optional[Any] = None, lnpost: Optional[Callable]=None,
            population: Optional[ndarray] = None, initial_population: Optional[ndarray] = None) -> None:
        """Fit the spectroscopic light curves jointly using Differential Evolution.

        Fit the spectroscopic light curves jointly for `niter` iterations using Differential Evolution.

        Parameters
        ----------
        niter : int, optional
            Number of iterations for optimization. Default is 200.
        npop : int, optional
            Population size for optimization. Default is 150.
        pool : multiprocessing.Pool, optional
            Multiprocessing pool for parallel optimization. Default is None.
        lnpost : callable, optional
            Log posterior function for optimization. Default is None.
        """
        if population is not None:
            x0 = population
            npop = x0.shape[0]
        else:
            if self._tsa.de is None and initial_population is not None:
                x0 = initial_population
                npop = x0.shape[0]
            elif self._tsa._de_population is not None:
                x0 = self._tsa._de_population
                npop = x0.shape[0]
            else:
                if npop is None:
                    raise ValueError("'npop' cannot be None when starting global optimization from the white light curve fit.'")
                if npop <= 2*self._tsa.ndim:
                    raise ValueError("'npop' should be at least two times the number of free model parameters.")

                pv0 = self._wa._local_minimization.x
                x0 = self._tsa.ps.sample_from_prior(npop)
                x0[:, 0] = normal(pv0[2], 0.05, size=npop)
                x0[:, 1] = normal(pv0[0], 1e-4, size=npop)
                x0[:, 2] = normal(pv0[1], 1e-5, size=npop)
                x0[:, 3] = clip(normal(pv0[3], 0.01, size=npop), 0.0, 1.0)
                x0[:, self._tsa._sl_rratios] = normal(sqrt(pv0[4]), 0.001, size=(npop, self.nk))

        self._tsa.optimize_global(niter=niter, npop=npop, population=x0, pool=pool, lnpost=lnpost,
                                  vectorize=(pool is None))
        self.de = self._tsa.de

    def sample(self, niter: int = 500, thin: int = 10, repeats: int = 1, pool=None, lnpost=None, leave=True, save=False, use_tqdm: bool = True):
        """Sample the posterior distribution using the emcee MCMC sampler.

        Parameters
        ----------
        niter : int, optional
            Number of iterations in the MCMC sampling. Default is 500.
        thin : int, optional
            Thinning factor for the MCMC samples. Default is 10.
        repeats : int, optional
            Number of repeated iterations in the MCMC sampling. Default is 1.
        pool : object, optional
            Parallel processing pool object to use for parallelization. Default is None.
        lnpost : function, optional
            Log posterior function that takes a parameter vector as input and returns the log posterior probability.
            Default is None.
        leave : bool, optional
            Whether to leave the progress bar visible after sampling is finished. Default is True.
        save : bool, optional
            Whether to save the MCMC samples to disk. Default is False.
        use_tqdm : bool, optional
            Whether to use tqdm progress bar during sampling. Default is True.

        """
        self._tsa.sample_mcmc(niter=niter, thin=thin, repeats=repeats, pool=pool, lnpost=lnpost,
                              vectorize=(pool is None), leave=leave, save=save, use_tqdm=use_tqdm)
        self.sampler = self._tsa.sampler

    def reset_sampler(self) -> None:
        """Reset the MCMC sampler

        Reset the MCMC sampler by clearing the Monte Carlo chains and setting the sampler to None.
        """
        self._tsa._mc_chains = None
        self._tsa.sampler = None

    def plot_transmission_spectrum(self, result: Optional[str] = None, ax: Axes = None, xscale: Optional[str] = None,
                                   xticks=None, ylim=None,  plot_resolution: bool = True) -> Figure:
        """Plot the transmission spectrum.

        Parameters
        ----------
        result : Optional[str]
            The type of result to plot. Can be 'fit', 'mcmc', or None. If None, the default behavior is to use 'mcmc' if
            the MCMC sampler has been run, otherwise 'fit'. Default is None.
        ax : Axes
            The matplotlib Axes object to plot on. If None, a new figure and axes will be created. Default is None.
        xscale : Optional[str]
            The scale of the x-axis. Can be 'linear', 'log', 'symlog', 'logit', or None. If None, the default behavior is to
            use the scale of the current axes. Default is None.
        xticks
            The tick locations for the x-axis. If None, the default behavior is to use the tick locations of the current axes.
        ylim
            The limits for the y-axis. If None, the default behavior is to use the limits of the current axes.
        plot_resolution : bool
            Whether to plot the resolution of the transmission spectrum as vertical lines. Default is True.

        Returns
        -------
        Figure
            The matplotlib Figure object of the plotted transmission spectrum.

        """
        if result is None:
            result = 'mcmc' if self._tsa.sampler is not None else 'fit'
        if result not in ('fit', 'mcmc'):
            raise ValueError("Result must be either 'fit', 'mcmc', or None")
        if result == 'mcmc' and self._tsa._mc_chains is None:
            raise ValueError("Cannot plot posterior solution before running the MCMC sampler.")

        fig, ax = subplots() if ax is None else (ax.get_figure(), ax)

        ix = argsort(self.wavelength)

        if result == 'fit':
            pv = self._tsa._de_population[self._tsa._de_imin]
            ar = 1e2 * squeeze(self._tsa._eval_k(pv[self._tsa._sl_rratios])) ** 2
            ax.plot(self.wavelength[ix], ar[ix], c='k')
            ax.plot(self._tsa.k_knots, 1e2 * pv[self._tsa._sl_rratios] ** 2, 'k.')
        else:
            df = pd.DataFrame(self._tsa._mc_chains.reshape([-1, self._tsa.ndim]), columns=self._tsa.ps.names)
            ar = 1e2 * self._tsa._eval_k(df.iloc[:, self._tsa._sl_rratios]) ** 2
            ax.fill_between(self.wavelength[ix], *percentile(ar[:, ix], [16, 84], axis=0), alpha=0.25)
            ax.plot(self.wavelength[ix], median(ar, 0)[ix], c='k')
            ax.plot(self.k_knots, 1e2*median(df.iloc[:, self._tsa._sl_rratios].values, 0)**2, 'k.')
        setp(ax, ylabel='Transit depth [%]', xlabel=r'Wavelength [$\mu$m]', xlim=self.wavelength[[0, -1]], ylim=ylim)

        if plot_resolution:
            yl = ax.get_ylim()
            ax.vlines(self.wavelength, yl[0], yl[0]+0.02*(yl[1]-yl[0]), ec='k')
        if xscale is not None:
            ax.set_xscale(xscale)
        if xticks is not None:
            ax.set_xticks(xticks, labels=xticks)
        return ax.get_figure()

    def plot_limb_darkening_parameters(self, result: Optional[str] = None, axs: Optional[tuple[Axes, Axes]] = None) -> Figure | None:
        """Plot the limb darkening parameters.

        Parameters
        ----------
        result : str, optional
            The type of result to plot. Can be 'fit', 'mcmc', or None. If None, the default behavior is to use 'mcmc' if
            the MCMC sampler has been run, otherwise 'fit'. Default is None.
        axs : Array-like of matplotlib.axes.Axes with a shape [2], optional
            The axes to plot the limb darkening parameters on. If None, a new figure with subplots will be created.
            Default is None.

        Returns
        -------
        fig : matplotlib.figure.Figure
            The figure containing the plot of the limb darkening parameters.

        Raises
        ------
        ValueError
            If the limb darkening model is not supported.
            If the result is not 'fit', 'mcmc', or None.
            If the result is 'mcmc' and the MCMC sampler has not been run.

        Notes
        -----
        This method plots the limb darkening parameters for two-parameter limb darkening models. It supports only
        quadratic, quadratic-tri, power-2, and power-2-pm models.
        """
        if not self._tsa.ldmodel in ('quadratic', 'quadratic-tri', 'power-2', 'power-2-pm'):
            return None

        if axs is None:
            fig, axs = subplots(1, 2, sharey='all', figsize=(13,4))
        else:
            fig = axs[0].get_figure()

        ldp1 = array([[p.prior.mean, p.prior.std] for p in self.ps[self._tsa._sl_ld][::2]])
        ldp2 = array([[p.prior.mean, p.prior.std] for p in self.ps[self._tsa._sl_ld][1::2]])

        if result is None:
            result = 'mcmc' if self._tsa.sampler is not None else 'fit'
        if result not in ('fit', 'mcmc'):
            raise ValueError("Result must be either 'fit', 'mcmc', or None")
        if result == 'mcmc' and self._tsa.sampler is None:
            raise ValueError("Cannot plot posterior solution before running the MCMC sampler.")

        ix = argsort(self.wavelength)

        if result == 'fit':
            pv = self._tsa._de_population[self._tsa._de_imin]
            ldc = self._tsa._eval_ldc(pv)[0]
            axs[0].plot(self._tsa.ld_knots, pv[self._tsa._sl_ld][0::2], 'ok')
            axs[0].plot(self.wavelength[ix], ldc[:,0][ix])
            axs[1].plot(self._tsa.ld_knots, pv[self._tsa._sl_ld][1::2], 'ok')
            axs[1].plot(self.wavelength[ix], ldc[:,1][ix])
        else:
            df = pd.DataFrame(self._tsa._mc_chains.reshape([-1, self._tsa.ndim]), columns=self.ps.names)
            ldc = df.iloc[:,self._tsa._sl_ld]

            ld1m = median(ldc.values[:,::2], 0)
            ld1e = ldc.values[:,::2].std(0)
            ld2m = median(ldc.values[:,1::2], 0)
            ld2e = ldc.values[:,1::2].std(0)

            ldc = self._tsa._eval_ldc(df.values)
            ld1p = percentile(ldc[:,:,0], [50, 16, 84], axis=0)
            ld2p = percentile(ldc[:,:,1], [50, 16, 84], axis=0)

            axs[0].fill_between(self.wavelength[ix], ld1p[1, ix], ld1p[2, ix], alpha=0.5)
            axs[0].plot(self.wavelength[ix], ld1p[0][ix], 'k')
            axs[1].fill_between(self.wavelength[ix], ld2p[1, ix], ld2p[2, ix], alpha=0.5)
            axs[1].plot(self.wavelength[ix], ld2p[0][ix], 'k')

            axs[0].errorbar(self._tsa.ld_knots, ld1m, ld1e, fmt='ok')
            axs[1].errorbar(self._tsa.ld_knots, ld2m, ld2e, fmt='ok')

        axs[0].plot(self._tsa.ld_knots, ldp1[:,0] + ldp1[:,1], ':', c='C0')
        axs[0].plot(self._tsa.ld_knots, ldp1[:,0] - ldp1[:,1], ':', c='C0')
        axs[1].plot(self._tsa.ld_knots, ldp2[:,0] + ldp2[:,1], ':', c='C0')
        axs[1].plot(self._tsa.ld_knots, ldp2[:,0] - ldp2[:,1], ':', c='C0')

        setp(axs, xlim=self.wavelength[[0,-1]], xlabel=r'Wavelength [$\mu$m]')
        setp(axs[0], ylabel='Limb darkening coefficient 1')
        setp(axs[1], ylabel='Limb darkening coefficient 2')
        return fig

    def plot_residuals(self, result: Optional[str] = None, ax: None | Axes | Iterable[Axes] = None,
                       pmin: float = 1, pmax: float = 99,
                       show_names: bool = False, cmap = None) -> Figure:
        """Plot the model residuals.

        Parameters
        ----------
        result : Optional[str], default=None
            The result type to plot. Must be either 'fit', 'mcmc', or None.
        ax : Axes, default=None
            The axes object to plot on. If None, a new figure and axes will be created.
        pmin : float, default=1
            The lower percentile to use when setting the color scale of the residuals image.
        pmax : float, default=99
            The upper percentile to use when setting the color scale of the residuals image.

        Returns
        -------
        Figure
            The figure object containing the plotted residuals.

        Raises
        ------
        ValueError
            If result is not one of 'fit', 'mcmc', or None.
        ValueError
            If result is 'mcmc' but the MCMC sampler has not been run.

        """
        if result not in ('fit', 'mcmc', None):
            raise ValueError("Result must be either 'fit', 'mcmc', or None")

        if result is None:
            if self._tsa._mc_chains is not None:
                result = 'mcmc'
            elif self._tsa._de_population is not None:
                result = 'fit'
            else:
                raise ValueError("Cannot plot residuals before running either the optimizer or the MCMC sampler.")

        if isinstance(self.data, TSData):
            nrows = 1
        else:
            nrows = self.data.ngroups

        if ax is None:
            fig, axs = subplots(nrows, 1, sharex='all', squeeze=False) if ax is None else (ax.figure, ax)
            axs = axs[:, 0]
        else:
            axs = [ax] if isinstance(ax, Axes) else ax
            if len(axs) != self.data.ngroups:
                raise ValueError("The number of axes must match the number of groups in the data.")
            fig = axs[0].figure

        if result == 'fit':
            pv = self._tsa._de_population[self._tsa._de_imin]
        else:
            pv = median(self._tsa._mc_chains.reshape([-1, self._tsa.ndim]), 0)

        fmodel = squeeze(self._tsa.flux_model(pv))
        residuals = self.fluxes - fmodel
        pp = percentile(residuals, [pmin, pmax])
        self.data.plot(ax = axs if isinstance(self.data, TSDataSet) else axs[0],
                       data=residuals, vmin=pp[0], vmax=pp[1], cmap=cmap)

        tc = self.transit_center
        td = self.transit_duration

        for ax in axs:
            for i in range(2):
                ax.axvline(tc + (-1) ** i * 0.5 * td - self._tref, c='w', ymax=0.05, lw=5)
                ax.axvline(tc + (-1) ** i * 0.5 * td - self._tref, c='w', ymin=0.95, lw=5)
                ax.axvline(tc + (-1) ** i * 0.5 * td - self._tref, c='k', ymax=0.05, lw=1)
                ax.axvline(tc + (-1) ** i * 0.5 * td - self._tref, c='k', ymin=0.95, lw=1)
            if not show_names:
                ax.set_title("")

        if isinstance(fig, Figure):
            fig.tight_layout()
        return fig

    def plot_fit(self, result: Optional[str] = None, figsize: Optional[tuple[float, float]]=None, res_args=None, trs_args=None) -> Figure:
        """Plot either the best-fit model or the posterior model.

        Parameters
        ----------
        result : Optional[str]
            Should be "fit", "mcmc", or None. Default is None.
        figsize : Optional[Tuple[float, float]]
            The size of the figure in inches. Default is None.
        res_args : Optional[Dict[str, Any]]
            Additional arguments for plotting residuals. Default is None.
        trs_args : Optional[Dict[str, Any]]
            Additional arguments for plotting transmission spectrum. Default is None.

        Returns
        -------
        fig : matplotlib.figure.Figure
            The plotted figure containing the residual plot, transmission spectrum plot, and limb darkening parameters plot.
        """
        if trs_args is None:
            trs_args = {}
        if res_args is None:
            res_args = {}

        fig = figure(layout='constrained', figsize=figsize)
        fts, fbelow = fig.subfigures(2, 1, hspace=0.07)
        fres, fldc = fbelow.subfigures(1, 2, wspace=0.05, width_ratios=(0.4, 0.6))
        axts = fts.add_subplot()
        axs_res = [fres.add_subplot(self.data.ngroups, 1, i+1) for i in range(self.data.ngroups)]
        axs_ldc = (fldc.add_subplot(2, 1, 1), fldc.add_subplot(2, 1, 2))

        self.plot_transmission_spectrum(result=result, ax=axts, **trs_args)
        self.plot_residuals(result=result, ax=axs_res, **res_args)
        self.plot_limb_darkening_parameters(result=result, axs=axs_ldc)
        fts.suptitle('Transmission spectrum')
        fres.suptitle('Residuals')
        fldc.suptitle('Limb darkening')
        setp(axs_ldc[0].get_xticklabels(), visible=False)
        for i in range(self.data.ngroups-1):
            setp(axs_res[i].get_xticklabels(), visible=False)
        setp(axs_ldc[0], xlabel="", ylabel='LDC 1', ylim=(0.18, 1.02))
        setp(axs_ldc[1], ylabel='LDC 2', ylim=(0.18, 1.02))
        fig.align_labels()
        return fig

    def get_transmission_spectrum(self) -> pd.DataFrame:
        """Get the posterior transmission spectrum.

        Returns:
            pd.DataFrame: A DataFrame containing the transmission spectrum with its uncertainties.

        Raises:
            ValueError: If the MCMC sampler has not been run before calculating the transmission spectrum.
        """
        if self._tsa._mc_chains is None:
            raise ValueError("Cannot calculate posterior transmission spectrum before running the MCMC sampler.")
        df = pd.DataFrame(self._tsa._mc_chains.reshape([-1, self._tsa.ndim]), columns=self._tsa.ps.names)
        ar = self._tsa._eval_k(df.iloc[:, self._tsa._sl_rratios])**2
        pt = percentile(ar, [50, 16, 84], 0)
        return pd.DataFrame(r_[pt[0:1], abs(pt[1:]-pt[0]).mean(0)[newaxis, :], pt[1:]-pt[0]].T,
                            columns='depth depth_e depth_eneg depth_epos'.split(),
                            index=pd.Index(self.wavelength, name='wavelength'))

    def save(self, overwrite: bool = False) -> None:
        """Saves the EasyTS analysis to a FITS file.

        Parameters
        ----------
        overwrite : bool, optional
            Flag indicating whether to overwrite an existing file with the same name.
        """
        pri = pf.PrimaryHDU()
        pri.header['name'] = self.name
        pri.header['t0'] = self.transit_center
        pri.header['t14'] = self.transit_duration
        pri.header['ndgroups'] = self.data.ngroups

        pr = pf.ImageHDU(name='priors')
        priors = [pickle.dumps(p) for p in self.ps]
        pr.header['priors'] = json.dumps(codecs.encode(pickle.dumps(priors), "base64").decode())

        if isinstance(self._tsa.ldmodel, LDTkLD):
            ldm = self._tsa.ldmodel
            pri.header['ldmodel'] = 'ldtk'
            pri.header['ldtkld'] = json.dumps(codecs.encode(pickle.dumps((ldm.sc.filters, ldm.sc.teff, ldm.sc.logg,
                                                                          ldm.sc.metal, ldm.dataset)), "base64").decode())
        else:
            pri.header['ldmodel'] = self._tsa.ldmodel

        k_knots = pf.ImageHDU(self._tsa.k_knots, name='k_knots')
        ld_knots = pf.ImageHDU(self._tsa.ld_knots, name='ld_knots')
        hdul = pf.HDUList([pri, k_knots, ld_knots, pr])

        for i, d in enumerate(self.data.data):
            flux = pf.ImageHDU(d.fluxes, name=f'flux_{i}')
            flux.header['name'] = d.name
            ferr = pf.ImageHDU(d.errors, name=f'ferr_{i}')
            wave = pf.ImageHDU(d.wavelength, name=f'wavelength_{i}')
            time = pf.ImageHDU(d.time, name=f'time_{i}')
            hdul.extend([time, wave, flux, ferr])

        if self._tsa.de is not None:
            de = pf.BinTableHDU(Table(self._tsa._de_population, names=self.ps.names), name='DE')
            de.header['npop'] = self._tsa.de.n_pop
            de.header['ndim'] = self._tsa.de.n_par
            de.header['imin'] = self._tsa.de.minimum_index
            hdul.append(de)

        if self._tsa.sampler is not None:
            mc = pf.BinTableHDU(Table(self._tsa.sampler.flatchain, names=self.ps.names), name='MCMC')
            mc.header['npop'] = self._tsa.sampler.nwalkers
            mc.header['ndim'] = self._tsa.sampler.ndim
            hdul.append(mc)

        hdul.writeto(f"{self.name}.fits", overwrite=True)

    def create_initial_population(self, n: int, source: str, add_noise: bool = True) -> ndarray:
        """Create an initial parameter vector population for the DE optimisation.

        Parameters
        ----------
        n : int
            Number of parameter vectors in the population.
        source : str
            Source of the initial population. Must be either 'fit' or 'mcmc'.
        add_noise : bool, optional
            Flag indicating whether to add noise to the initial population. Default is True.

        Returns
        -------
        numpy.ndarray
            The initial population.

        Raises
        ------
        ValueError
            If the source is not 'fit' or 'mcmc'.
        """
        return self._tsa.create_initial_population(n, source, add_noise)

    def add_noise_to_solution(self, result: str = 'fit') -> None:
        """Add noise to the global optimization result or MCMC parameter posteriors.

        Add noise to the global optimization result or MCMC parameter posteriors. You may want to do this if you
        create a new analysis from another one, for example, by adding radius ratio knots or changing the intrinsic
        data resolution.

        Parameters
        ----------
        result : str, optional
            Determines which result to add noise to. Default is 'fit'.

        Raises
        ------
        ValueError
            If the 'result' argument is not 'fit' or 'mcmc'.
        """
        if result == 'fit':
            pvp = self._tsa._de_population[:, :].copy()
        elif result == 'mcmc':
            pvp = self._tsa._mc_chains[:, -1, :].copy()
        else:
            raise ValueError("The 'result' argument must be either 'fit' or 'mcmc'")

        npv = pvp.shape[0]
        pvp[:, 0] += normal(0, 0.005, size=npv)
        pvp[:, 1] += normal(0, 0.001, size=npv)
        pvp[:, 3] += normal(0, 0.005, size=npv)
        pvp[:, self._tsa._sl_rratios] += normal(0, 1, pvp[:, self._tsa._sl_rratios].shape) * 0.002 * pvp[:, self._tsa._sl_rratios]
        pvp[:, self._tsa._sl_ld] += normal(0, 1, pvp[:, self._tsa._sl_ld].shape) * 0.002 * pvp[:, self._tsa._sl_ld]

        if result == 'fit':
            self._tsa._de_population[:, :] = pvp
        else:
            pvp = self._tsa._mc_chains[:, -1, :] = pvp

    def optimize_gp_hyperparameters(self,
                                    log10_sigma_bounds: float | tuple[float, float] = (-5, -2),
                                    log10_rho_bounds: float | tuple[float, float] = (-5, 0),
                                    log10_sigma_prior=None, log10_rho_prior=None,
                                    npop: int = 10, niter: int = 100, subset = None):
        """Optimize the Matern-3/2 kernel Gaussian Process hyperparameters.

        Parameters
        ----------
        log10_sigma_bounds : float or tuple[float, float], optional
            The bounds for the log10 of the sigma hyperparameter. If float is provided, the parameter will be
            fixed to the given value. Default is (-5, -2).
        log10_rho_bounds : float or tuple[float, float], optional
            The bounds for the log10 of the rho hyperparameter. If float is provided, the parameter will be fixed
            to the given value. Default is (-5, 0).
        log10_sigma_prior : object or iterable, optional
            The prior distribution for the sigma hyperparameter expressed as an object with a `logpdf` method
            or as an iterable containing the mean and standard deviation of the prior distribution. Default is None.
        log10_rho_prior : object or iterable, optional
            The prior distribution for the rho hyperparameter expressed as an object with a `logpdf` method
            or as an iterable containing the mean and standard deviation of the prior distribution. Default is None.
        npop : int, optional
            The population size for the differential evolution optimizer. Default is 10.
        niter : int, optional
            The number of iterations for the differential evolution optimization process. Default is 100.
        subset : iterable or float, optional
            The subset used for the optimization process. If `subset` is a float, a random subset of size
            `0.5 * self.npb` is used. If `subset` is an iterable, it must contain the indices of the subset.
            Default is None.

        Returns
        -------
        tuple[float, float]
            The optimized values for the log10 of the sigma and rho hyperparameters.
        float
            The fitness value.

        Raises
        ------
        ValueError
            If `subset` is not an iterable or a float.
        ValueError
            If `log10_sigma_prior` is not an object with a `logpdf` method or iterable.
        ValueError
            If `log10_rho_prior` is not an object with a `logpdf` method or iterable.

        Notes
        -----
        - The Gaussian Process is reconfigured with the optimal hyperparameters. Any previous kernels are overwritten.
        """

        if self._tsa.noise_model != 'fixed_gp':
            raise ValueError("The noise model must be set to 'fixed_gp' before the hyperparameter optimization.")

        sb = log10_sigma_bounds if isinstance(log10_sigma_bounds, Iterable) else [log10_sigma_bounds-1, log10_sigma_bounds+1]
        rb = log10_rho_bounds if isinstance(log10_rho_bounds, Iterable) else [log10_rho_bounds-1, log10_rho_bounds+1]
        bounds = array([sb, rb])

        if subset is not None:
            if isinstance(subset, float):
                ids = sort(permutation(self.npb)[:int(0.5*self.npb)])
            elif isinstance(subset, Iterable):
                ids = array(subset, int)
            else:
                raise ValueError("subset must be either an iterable or a float.")
        else:
            ids = arange(self.npb)

        class DummyPrior:
            def logpdf(self, x):
                return 0.0

        if log10_sigma_prior is not None:
            if isinstance(log10_sigma_prior, Iterable):
                sp = norm(*log10_sigma_prior)
            elif hasattr(log10_sigma_prior, 'logpdf'):
                sp = log10_sigma_prior
            else:
                raise ValueError('Bad sigma_prior')
        else:
            sp = DummyPrior()

        if log10_rho_prior is not None:
            if isinstance(log10_rho_prior, Iterable):
                rp = norm(*log10_rho_prior)
            elif hasattr(log10_rho_prior, 'logpdf'):
                rp = log10_rho_prior
            else:
                raise ValueError('Bad rho_prior')
        else:
            rp = DummyPrior()

        npb = ids.size
        time = (tile(self.time[newaxis, self.ootmask], (npb, 1)) + arange(npb)[:, newaxis]).ravel()
        flux = (self.fluxes[ids, :][:, self.ootmask]).ravel() - 1
        ferr = (self._tsa.ferr[ids, :][:, self.ootmask]).ravel()
        gp = GaussianProcess(terms.Matern32Term(sigma=flux.std(), rho=0.1))

        def nll(log10x):
            x = 10**log10x
            if any(log10x < bounds[:,0]) or any(log10x > bounds[:,1]):
                return inf
            gp.kernel = terms.Matern32Term(sigma=x[0], rho=x[1])
            gp.compute(time, yerr=ferr, quiet=True)
            return -(gp.log_likelihood(flux) + sp.logpdf(log10x[0]) + rp.logpdf(log10x[1]))

        de = DiffEvol(nll, bounds, npop, min_ptp=0.2)
        if isinstance(log10_sigma_bounds, float):
            de.population[:, 0] = log10_sigma_bounds
        if isinstance(log10_rho_bounds, float):
            de.population[:, 1] = log10_rho_bounds

        de.optimize(niter)

        x = de.minimum_location
        gp.kernel = terms.Matern32Term(sigma=10**x[0], rho=10**x[1])
        gp.compute(self._tsa._gp_time, yerr=self._tsa._gp_ferr, quiet=True)
        self._tsa._gp = gp
        return 10**x, de._fitness.ptp()
