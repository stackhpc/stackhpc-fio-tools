# .config/matplotlib/matplotlibrc line backend : Agg
import matplotlib.pyplot as plt
from pathlib2 import Path
import numpy as np
import errno
import json
import math
import os
import time
import pdb

class ClatGrid:
    min_x = np.inf
    max_x = 0
    min_y = np.inf
    max_y = 0.
    grid_y = 0
    io_bs = {}
    io_density = {}
    iops_bs = {}
    timescale = 'us'
    logscale = False
    ts_dict = {
        'us': {'divider': 10**3, 'label':'\mu s'},
        'ms': {'divider': 10**6, 'label':'m s'},
    }
    
    def __init__( self, input_dirs, output_dir, granularity, force, mode, skip_bs=[], logscale=False, timescale='us', max_bs=65536):
        self.grid_y = granularity
        self.logscale = logscale
        self.timescale = timescale
        self.max_bs = max_bs
        self.divider = self.ts_dict[timescale]['divider']
        self.label = self.ts_dict[timescale]['label']
        self.skip_bs = skip_bs
        self.input_dirs = input_dirs
        self.output_dir = output_dir
        self.mode = mode
        
        ensure_output_dir(output_dir, force)
        for input_dir in input_dirs:
            print "Scanning for fio data in %s" % input_dir
            self.populate(input_dir, output_dir)
        self.aggregate_and_normalise()

    # Each series is indexed by the IO size (and the test mode)
    # Multiple client series are stored independently at this stage
    # and will be gridded, aggregated and normalised later on.
    def add_series( self, bs, iops_total, clat_data ):

        # Paranoia: Check the iops_total matches the sum of all bins
        if sum(clat_data.values()) != iops_total:
            print "I/O size %d: sum of histogram bins is %d, expected %d" % (bs, sum(clat_data.values()), iops_total)
            raise ValueError

        # Construct a dict of floating-point IO latencies
        bs_data = {}
        for y_str, z_str in clat_data.iteritems():
            y = float(y_str)
            y /= self.divider
            if self.logscale:
                y = math.log(y, 10)
            z = float(z_str)
            bs_data[y] = z
            self.min_y = min(self.min_y, y)
            self.max_y = max(self.max_y, y)

        # Update grid-boundary metrics
        x = int(math.log(bs, 2))
        self.min_x = min(self.min_x, x)
        self.max_x = max(self.max_x, x)

        # Add the data to any existing data sets for this blocksize
        if x not in self.io_bs:
            self.io_bs[x] = []
            self.iops_bs[x] = 0
        self.io_bs[x] += [bs_data]
        self.iops_bs[x] += iops_total


    def aggregate_and_normalise( self ):
        ''' We may have sampled multiple results per blocksize.
            Generate a weighted normalisation across all readings.
            This must be done once all results have been added. '''

        # For each blocksize
        for bs, bs_results in self.io_bs.iteritems():
            z_total = float(self.iops_bs[bs])
            io_density = []

            # For each fio result in this blocksize
            for bs_data in bs_results:
                result_norm = {}
                prev_y = 0.0

                # For each datapoint in the result
                for y in sorted(bs_data.keys()):

                    # Process the IOs in order to construct IO frequency densities
                    z_norm = bs_data[y] / z_total
                    delta_y = y - prev_y
                    io_density_y = z_norm / delta_y
                    io_density += [dict(lower=prev_y, upper=y, density=io_density_y)]
                    prev_y = y

            # The generated list of I/O frequency density ranges
            # is suitable for resampling on a regularised grid
            self.io_density[bs] = io_density


    def fit_to_grid( self ):
        ''' Once all fio latency histograms have been submitted, 
            reinterpolate the data to a regular grid spacing
            to enable aggregation and plotting. '''

        bin_y = self.max_y / self.grid_y

        # Make coordinate arrays.
        #yi = np.logspace(0.0, np.log(self.max_y), bin_y)
        yi = np.arange(0.0, self.max_y, bin_y)
        nrow, ncol = (self.grid_y, self.max_x - self.min_x + 1)
        grid = np.zeros((nrow, ncol), dtype=np.dtype('double'))
        
        # Perform the gridding interpolation
        for x in sorted(self.io_bs.keys()):
            col = x - self.min_x
            io_density = self.io_density[x]
            io_density_check = 0.0          # Paranoia
            grid_check = 0.0                # Paranoia
            for D in io_density:
                for row in range(int(math.floor(D['lower'] / bin_y)), nrow):

                    grid_y_lower = yi[row]
                    grid_y_upper = grid_y_lower + bin_y

                    # Non-overlap: below or above?
                    if grid_y_upper < D['lower']:
                        continue
                    if grid_y_lower > D['upper']:
                        break

                    # Determine the extent of overlap
                    overlap_lower = max(grid_y_lower, D['lower'])
                    overlap_upper = min(grid_y_upper, D['upper'])
                    overlap_range = overlap_upper - overlap_lower

                    grid[row, col] += D['density'] * overlap_range / bin_y

                    # Paranoia
                    grid_check += D['density'] * overlap_range

                # Paranoia
                io_density_check += D['density']*(D['upper']-D['lower'])

            # Paranoia
            if io_density_check < 0.999 or io_density_check > 1.001 or grid_check < 0.999 or grid_check > 1.001:
                print "CHECK FAILED: blocksize %d cumulative density %f cumulative grid %f" % (2**x, io_density_check, grid_check)
                raise ValueError

        return grid


    # OK we have enough data, construct the grid and populate with interpolations
    def plot_data(self, output_dir, filename='blob.png',cmap='copper',colorbar=False):
        grid = self.fit_to_grid()
        
        fig = plt.figure()
        # Find maximum value on Grid
        max_z = grid.max()
        nrow, ncol = grid.shape

        # Set empty bins to NaN to ensure they do not get plotted
        for row in range(nrow):
            for col in range(ncol):
                if grid[row, col] == 0.0:
                    grid[row, col] = np.nan

        extent = (self.min_x-0.5, self.max_x+0.5, self.min_y, self.max_y)
        plt.imshow(grid, extent=extent, cmap=cmap, origin='lower', vmin=0.0, vmax=max_z, aspect='auto', interpolation='none')
        if self.logscale:
            #plt.ylim(5,7)
            plt.ylabel('log (commit latency) - $%s$' % self.label)
        else:
            #plt.ylim(10**5,10**7)
            plt.ylabel('commit latency - $%s$' % self.label)
        plt.xlabel(r'block size - $2^n$')
        if colorbar:
            plt.colorbar(label='relative frequency per blocksize')
        filename = "%s/%s" % (output_dir, filename)
        plt.savefig(filename, dpi=150, orientation='landscape', transparent=False)
        print 'Plotting to %s' % filename
        return fig

    def populate(self, input_dir, output_dir):
        # For each blocksize found, emit data from each listed job
        # FIXME: need to incorporate hostname and dataset name in the results
        # Emit bandwidth data points in column format
        fio_file_list = get_fio_file_list(input_dir)
        fio_results = get_fio_results(fio_file_list)
        bs_list = sorted(fio_results.keys())
        for bs in bs_list:
            for bs_job in fio_results[bs]['jobs']:

                # Read and write bandwidth as a function of I/O size
                fpath = output_dir/(self.mode+'-bandwidth.dat')
                with fpath.open('a+') as job_fd:
                    job_fd.write(u'{0:8}\t{1:8}\t{2:8}\n'
                        .format(bs, bs_job[self.mode]['bw'], bs_job['write']['bw']))

                # IOPS and IO latency percentiles as a function of I/O size
                fpath = output_dir/(self.mode+'-iops-latency.dat')
                with fpath.open('a+') as job_fd:
                    job_fd.write(u'{:8}\t{:8}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n'
                        .format(bs, bs_job[self.mode]['iops'],
                            bs_job[self.mode]['clat_ns']['percentile']["1.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["5.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["10.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["20.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["30.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["40.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["50.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["60.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["70.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["80.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["90.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["95.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["99.000000"],
                            bs_job[self.mode]['clat_ns']['percentile']["99.500000"],
                            bs_job[self.mode]['clat_ns']['percentile']["99.900000"],
                            bs_job[self.mode]['clat_ns']['percentile']["99.950000"],
                            bs_job[self.mode]['clat_ns']['percentile']["99.990000"]))

                # Write I/O completion latencies as a datafile of x y z datapoints
                fpath = output_dir/(self.mode+'-clat.dat')
                with fpath.open('a+') as job_fd:
                    # Need to transform string keys into integer to sort
                    for bin_ns in sorted([int(x) for x in bs_job[self.mode]['clat_ns']['bins'].keys()]):
                        bin_freq = bs_job[self.mode]['clat_ns']['bins'][str(bin_ns)]
                        job_fd.write(u'{:8}\t{:10}\t{:8}\n'.format(math.log(bs,2), bin_ns, bin_freq))
                    job_fd.write(u'\n')

                # Aggregate data from each dataset
                if bs <= self.max_bs and bs not in self.skip_bs:
                    self.add_series( int(bs), bs_job[self.mode]['total_ios'], bs_job[self.mode]['clat_ns']['bins'] ) 

                print "I/O size %8d, job %s: %d samples" % (bs, self.mode, bs_job[self.mode]['total_ios'])
            
        print "Aggregated data for %d I/Os, max latency %f %s" % (sum(self.iops_bs.values()), self.max_y if not self.logscale else 10**self.max_y, self.timescale)

def get_fio_file_list(input_dir):
    # List JSON files in the fio input directory
    try:
        return list(input_dir.iterdir())
    except OSError as E:
        print "Could not access input directory %s: %s" % (input_dir, os.strerror(E.errno))
        os.abort()

def ensure_output_dir(output_dir, force):
    # Check the status of the output directory
    output_dir.mkdir(parents=True, exist_ok=force)
    for p in output_dir.iterdir():
        if force:
            print "Deleting existing output data %s in output directory" % (p)
            p.unlink()
        else:
            print "Output directory %s is not empty: use --force to overwrite it" % (output_dir)
            os.abort()

def get_fio_results(fio_file_list):
    # Read in and parse the data files
    fio_results = {}
    for fio_file in fio_file_list:
        with fio_file.open('r') as fio_fd:
            try:
                fio_run_data = json.load(fio_fd)
                test_bs = int(fio_run_data['global options']['bs'])
                fio_results[test_bs] = fio_run_data
            except ValueError as E:
                print "Skipping %s: could not be parsed as JSON" % (fio_file)
                pass
            except KeyError as E:
                print "Skipping %s: data structure could not be parsed" % (fio_file)
                pass
    return fio_results
