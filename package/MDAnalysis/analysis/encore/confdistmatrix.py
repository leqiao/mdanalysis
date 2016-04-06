# confdistmatrix.py --- Conformational distance matrix calculator
# Copyright (C) 2014 Wouter Boomsma, Matteo Tiberti
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Distance Matrix calculation --- :mod:`MDAnalysis.analysis.ensemble.confdistmatrix`
=====================================================================


The module contains a base class to easily compute, using parallelization and
shared memory, matrices of conformational distance between the structures
stored in an Ensemble. A class to compute an RMSD matrix in such a way is also
available.

:Author: Matteo Tiberti, Wouter Boomsma, Tone Bengtsen
:Year: 2015--2016
:Copyright: GNU Public License v3
:Mantainer: Matteo Tiberti <matteo.tiberti@gmail.com>, mtiberti on github

.. versionadded:: 0.14.0

"""

from multiprocessing import Process, Array, RawValue
from numpy import (sum, average, transpose, dot, ones, asarray, mean,
                   float64, object, bool, array, int)
from ctypes import c_float
from getpass import getuser
from socket import gethostname
from datetime import datetime
from time import sleep

from ..align import rotation_matrix

from .cutils import PureRMSD
from .utils import TriangularMatrix, trm_indeces, AnimatedProgressBar



def conformational_distance_matrix(ensemble,
    conf_dist_function, selection="",
    superimposition_selection="", ncores=1, pairwise_align=True,
    mass_weighted=True, metadata=True, *args, **kwargs):
    """
    Run the conformational distance matrix calculation.
    args and kwargs are passed to conf_dist_function.

    Parameters
    ----------

    ensemble : encore.Ensemble.Ensemble object
        Ensemble object for which the conformational distance matrix will
        be computed.

    conf_dist_function : function object
        Function that fills the matrix with conformational distance
        values. See set_rmsd_matrix_elements for an example.

    pairwise_align : bool
        Whether to perform pairwise alignment between conformations

    mass_weighted : bool
        Whether to perform mass-weighted superimposition and metric
        calculation

    metadata : bool
        Whether to build a metadata dataset for the calculated matrix

    ncores : int
        Number of cores to be used for parallel calculation (default is 1)

    Returns
    -------

    conf_dist_matrix` : encore.utils.TriangularMatrix object
        Conformational distance matrix in triangular representation.

    """

    # Decide how many cores have to be used. Since the main process is
    # stopped while the workers do their job, ncores workers will be
    # spawned.

    if ncores < 1:
        ncores = 1

    # framesn: number of frames
    framesn = len(ensemble.trajectory.timeseries(
        ensemble.select_atoms(selection), format='fac'))

    # Prepare metadata recarray
    if metadata:
        metadata = array([(gethostname(),
                           getuser(),
                           str(datetime.now()),
                           ensemble.filename,
                           framesn,
                           pairwise_align,
                           selection,
                           mass_weighted)],
                         dtype=[('host', object),
                                ('user', object),
                                ('date', object),
                                ('topology file', object),
                                ('number of frames', int),
                                ('pairwise superimposition', bool),
                                ('superimposition subset', object),
                                ('mass-weighted', bool)])

    # Prepare alignment subset coordinates as necessary

    rmsd_coordinates = ensemble.trajectory.timeseries(
            ensemble.select_atoms(selection),
            format='fac')

    print "AA", rmsd_coordinates

    if pairwise_align:
        if superimposition_selection:
            subset_selection = superimposition_selection
        else:
            subset_selection = selection

        fitting_coordinates = ensemble.trajectory.timeseries(
            ensemble.select_atoms(subset_selection),
            format='fac')
    else:
        fitting_coordinates = None

    # Prepare masses as necessary
    if mass_weighted:
        masses = ensemble.select_atoms(selection).masses
        if pairwise_align:
            subset_masses = ensemble.select_atoms(subset_selection).masses
        else:
            subset_masses = None
    else:
        masses = ones((ensemble.trajectory.timeseries(
            ensemble.select_atoms(selection))[0].shape[0]))
        if pairwise_align:
            subset_masses = ones((fit_coords[0].shape[0]))
        else:
            subset_masses = None

    # matsize: number of elements of the triangular matrix, diagonal
    # elements included.
    matsize = framesn * (framesn + 1) / 2

    # Calculate the number of matrix elements that each core has to
    # calculate as equally as possible.
    if ncores > matsize:
        ncores = matsize
    runs_per_worker = [matsize / int(ncores) for x in range(ncores)]
    unfair_work = matsize % ncores
    for i in range(unfair_work):
        runs_per_worker[i] += 1

    # Splice the matrix in ncores segments. Calculate the first and the
    # last (i,j) matrix elements of the slices that will be assigned to
    # each worker. Each of them will proceed in a column-then-row order
    # (e.g. 0,0 1,0 1,1 2,0 2,1 2,2 ... )
    i = 0
    a = [0, 0]
    b = [0, 0]
    tasks_per_worker = []
    for n,r in enumerate(runs_per_worker):
        while i * (i - 1) / 2 < sum(runs_per_worker[:n + 1]):
            i += 1
        b = [i - 2,
             sum(runs_per_worker[0:n + 1]) - (i - 2) * (i - 1) / 2 - 1]
        tasks_per_worker.append((tuple(a), tuple(b)))
        if b[0] == b[1]:
            a[0] = b[0] + 1
            a[1] = 0
        else:
            a[0] = b[0]
            a[1] = b[1] + 1

    # Allocate for output matrix
    distmat = Array(c_float, matsize)

    # Prepare progress bar stuff and run it
    pbar = AnimatedProgressBar(end=matsize, width=80)
    partial_counters = [RawValue('i', 0) for i in range(ncores)]

    # Initialize workers. Simple worker doesn't perform fitting,
    # fitter worker does.
    
    workers = [Process(target=conf_dist_function, args=(
        tasks_per_worker[i],
        rmsd_coordinates,
        distmat,
        masses,
        fitting_coordinates,
        subset_masses,
        partial_counters[i],
        args,
        kwargs)) for i in range(ncores)]

    # Start & join the workers
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    
    # When the workers have finished, return a TriangularMatrix object
    return TriangularMatrix(distmat, metadata=metadata)


def set_rmsd_matrix_elements(tasks, coords, rmsdmat, masses, fit_coords=None,
                       fit_masses=None, pbar_counter=None, *args, **kwargs):

    '''
    RMSD Matrix calculator

    Parameters
    ----------

        tasks : iterator of int of length 2
    Given a triangular matrix, this worker will calculate RMSD
    values from element tasks[0] to tasks[1]. Since the matrix
    is triangular, the trm_indeces matrix automatically
    calculates the corrisponding i,j matrix indices.
    The matrix is written as an array in a row-major
    order (see the TriangularMatrix class for details).

    If fit_coords and fit_masses are specified, the structures
    will be superimposed before calculating RMSD, and fit_coords and fit_masses
    will be used to place both structures at their center of mass and 
    compute the rotation matrix. In this case, both fit_coords and fit_masses
    must be specified.

    coords : numpy.array
        Array of the ensemble coordinates

    masses : numpy.array
        Array of atomic masses, having the same order as the
        coordinates array

    rmsdmat : encore.utils.TriangularMatrix
        Memory-shared triangular matrix object

    fit_coords : numpy.array or None
        Array of the coordinates used for fitting

    fit_masses : numpy.array
        Array of atomic masses, having the same order as the
        fit_coords array

    pbar_counter : multiprocessing.RawValue
        Thread-safe shared value. This counter is updated at
        every cycle and used to evaluate the progress of
        each worker in a parallel calculation.
        '''

    print coords
    print fit_coords
    print masses
    print fit_masses

    if fit_coords is None and fit_masses is None:
        for i, j in trm_indeces(tasks[0], tasks[1]):
            summasses = sum(masses)
            rmsdmat[(i + 1) * i / 2 + j] = PureRMSD(coords[i].astype(float64),
                                                    coords[j].astype(float64),
                                                    coords[j].shape[0], 
                                                    masses,
                                                    summasses)

    elif fit_coords is not None and fit_coords is not None:
        for i, j in trm_indeces(tasks[0], tasks[1]):
            summasses = sum(masses)
            subset_weights = asarray(fit_masses) / mean(fit_masses)
            com_i = average(fit_coords[i], axis=0,
                            weights=fit_masses)
            translated_i = coords[i] - com_i
            subset1_coords = fit_coords[i] - com_i
            com_j = average(fit_coords[j], axis=0,
                            weights=fit_masses)
            translated_j = coords[j] - com_j
            subset2_coords = fit_coords[j] - com_j
            print "XX", coords.shape, translated_i.shape, coords[i].shape
            rotamat = rotation_matrix(subset1_coords, subset2_coords,
                                      subset_weights)[0]
            rotated_i = transpose(dot(rotamat, transpose(translated_i)))
            rmsdmat[(i + 1) * i / 2 + j] = PureRMSD(
                rotated_i.astype(float64), translated_j.astype(float64),
                coords[j].shape[0], masses, summasses)

    else: 
        raise TypeError("Both fit_coords and fit_masses must be specified \
                        if one of them is given")

    if pbar_counter is not None:
        pbar_counter.value += 1

def pbar_updater(pbar, pbar_counters, max_val, update_interval=0.2):
    '''Method that updates and prints the progress bar, upon polling
    progress status from workers.

    Attributes
    -----------

    pbar : encore.utils.AnimatedProgressBar object
        Progress bar object

    pbar_counters : list of multiprocessing.RawValue
        List of counters. Each worker is given a counter, which is updated
        at every cycle. In this way the _pbar_updater process can
        asynchronously fetch progress reports.

        max_val : int
        Total number of matrix elements to be calculated

        update_interval : float
        Number of seconds between progress bar updates

    '''

    val = 0
    while val < max_val:
        val = 0
        for c in pbar_counters:
            val += c.value
        pbar.update(val)
        pbar.show_progress()
        sleep(update_interval)
