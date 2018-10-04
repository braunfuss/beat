#!/usr/bin/env python
import os

# disable internal blas parallelisation as we parallelise over chains
os.environ["OMP_NUM_THREADS"] = "1"

import logging
import sys
import copy
import shutil

from optparse import OptionParser

from beat import heart, config, utility, inputf, backend
from beat.models import load_model, Stage, estimate_hypers, sample
from beat.backend import TextChain
from beat.sources import MTSourceWithMagnitude
from beat.utility import list2string

from pyrocko import model, util
from pyrocko.trace import snuffle
from pyrocko.gf import LocalEngine

from pyrocko.guts import load, dump


logger = logging.getLogger('beat')


km = 1000.


def d2u(d):
    return dict((k.replace('-', '_'), v) for (k, v) in d.items())


subcommand_descriptions = {
    'init':           'create a new EQ model project, use only event'
                      ' name to skip catalog search',
    'import':         'import data or results, from external format or '
                      'modeling results, respectively',
    'update':         'update configuration file',
    'sample':         'sample the solution space of the problem',
    'build_gfs':      'build GF stores',
    'clone':          'clone EQ model project into new directory',
    'plot':           'plot specified setups or results',
    'check':          'check setup specific requirements',
    'summarize':      'collect results and create statistics',
}

subcommand_usages = {
    'init':          'init <event_name> <event_date "YYYY-MM-DD"> '
                     '[options]',
    'import':        'import <event_name> [options]',
    'update':        'update <event_name> [options]',
    'sample':        'sample <event_name> [options]',
    'build_gfs':     'build_gfs <event_name> [options]',
    'clone':         'clone <event_name> <cloned_event_name> [options]',
    'plot':          'plot <event_name> <plot_type> [options]',
    'check':         'check <event_name> [options]',
    'summarize':     'summarize <event_name> [options]',
}

subcommands = subcommand_descriptions.keys()

program_name = 'beat'

usage = program_name + ''' <subcommand> <arguments> ... [options]
BEAT: Bayesian earthquake analysis tool
 Version 1.0beta
author: Hannes Vasyuara-Bathke
email: hannes.vasyura-bathke@kaust.edu.sa

Subcommands:

    init            %(init)s
    clone           %(clone)s
    import          %(import)s
    update          %(update)s
    build_gfs       %(build_gfs)s
    sample          %(sample)s
    summarize       %(summarize)s
    plot            %(plot)s
    check           %(check)s

To get further help and a list of available options for any subcommand run:

    beat <subcommand> --help

''' % d2u(subcommand_descriptions)


nargs_dict = {
    'init': 2,
    'clone': 2,
    'plot': 2,
    'import': 1,
    'update': 1,
    'build_gfs': 1,
    'sample': 1,
    'check': 1,
    'summarize': 1,
}

mode_choices = ['geometry', 'ffi']
supported_geodetic_formats = ['matlab', 'ascii', 'kite']
supported_samplers = ['SMC', 'Metropolis', 'PT']


def add_common_options(parser):
    parser.add_option(
        '--loglevel',
        action='store',
        dest='loglevel',
        type='choice',
        choices=('critical', 'error', 'warning', 'info', 'debug'),
        default='info',
        help='set logger level to '
             '"critical", "error", "warning", "info", or "debug". '
             'Default is "%default".')


def get_project_directory(args, options, nargs=1, popflag=False):

    larg = len(args)

    if larg == nargs - 1:
        project_dir = os.getcwd()
    elif larg == nargs:
        if popflag:
            name = args.pop(0)
        else:
            name = args[0]
        project_dir = os.path.join(os.path.abspath(options.main_path), name)
    else:
        project_dir = os.getcwd()

    return project_dir


def process_common_options(options, project_dir):
    util.ensuredir(project_dir)
    utility.setup_logging(
        project_dir, options.loglevel, logfilename='BEAT_log.txt')


def die(message, err=''):
    sys.exit('%s: error: %s \n %s' % (program_name, message, err))


def cl_parse(command, args, setup=None, details=None):
    usage = subcommand_usages[command]
    descr = subcommand_descriptions[command]

    if isinstance(usage, str):
        usage = [usage]

    susage = '%s %s' % (program_name, usage[0])
    for s in usage[1:]:
        susage += '\n%s%s %s' % (' ' * 7, program_name, s)

    description = descr[0].upper() + descr[1:] + '.'

    if details:
        description = description + ' %s' % details

    parser = OptionParser(usage=susage, description=description)

    if setup:
        setup(parser)

    add_common_options(parser)
    (options, args) = parser.parse_args(args)
    project_dir = get_project_directory(args, options, nargs_dict[command])
    process_common_options(options, project_dir)
    return parser, options, args


def load_config(fn):
    try:
        config = load(filename=fn)
        assert isinstance(config, config.BEATconfig)

    except IOError:
        die('cannot load BEAT config from file: %s' % fn)

    return config


def list_callback(option, opt, value, parser):
    out = [ival.lstrip() for ival in value.split(',')]
    setattr(parser.values, option.dest, out)


def command_init(args):

    def setup(parser):

        parser.add_option(
            '--min_mag', dest='min_mag', type=float,
            default=6.,
            help='Minimum Mw for event, for catalog search.'
                 ' Default: "6.0"')

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) for creating directory structure.'
                 '  Default: current directory ./')

        parser.add_option(
            '--datatypes',
            default=['geodetic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to include in the setup; "geodetic, seismic".')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--source_type', dest='source_type',
            choices=config.source_names,
            default='RectangularSource',
            help='Source type to solve for; %s'
                 '. Default: "RectangularSource"' % (
                     '", "'.join(name for name in config.source_names)))

        parser.add_option(
            '--n_sources', dest='n_sources', type='int',
            default=1,
            help='Integer Number of sources to invert for. Default: 1')

        parser.add_option(
            '--waveforms', type='string',
            action='callback', callback=list_callback,
            default=['any_P', 'any_S'],
            help='Waveforms to include in the setup; "any_P, any_S, slowest".')

        parser.add_option(
            '--sampler', dest='sampler',
            choices=supported_samplers,
            default='SMC',
            help='Sampling algorithm to sample the solution space of the'
                 ' general problem; %s. '
                 'Default: "SMC"' % list2string(supported_samplers))

        parser.add_option(
            '--hyper_sampler', dest='hyper_sampler',
            type='string', default='Metropolis',
            help='Sampling algorithm to sample the solution space of the'
                 ' hyperparameters only; So far only "Metropolis" supported.'
                 'Default: "Metropolis"')

        parser.add_option(
            '--use_custom', dest='use_custom',
            action='store_true',
            help='If set, a slot for a custom velocity model is being created'
                 ' in the configuration file.')

        parser.add_option(
            '--individual_gfs', dest='individual_gfs',
            action='store_true',
            help="If set, Green's Function stores will be created individually"
                 " for each station!")

    parser, options, args = cl_parse('init', args, setup=setup)

    la = len(args)

    if la > 2 or la < 1:
        logger.error('Wrong number of input arguments!')
        parser.print_help()
        sys.exit(1)

    if la == 2:
        name, date = args
    elif la == 1:
        logger.info(
            'Doing no catalog search for event information!')
        name = args[0]
        date = None

    return config.init_config(name, date,
                              main_path=options.main_path,
                              min_magnitude=options.min_mag,
                              datatypes=options.datatypes,
                              mode=options.mode,
                              source_type=options.source_type,
                              n_sources=options.n_sources,
                              waveforms=options.waveforms,
                              sampler=options.sampler,
                              hyper_sampler=options.hyper_sampler,
                              use_custom=options.use_custom,
                              individual_gfs=options.individual_gfs)


def command_import(args):

    command_str = 'import'

    def setup(parser):

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--results', dest='results', action='store_true',
            help='Import results from previous modeling step.')

        parser.add_option(
            '--datatypes',
            default=['geodetic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to import; "geodetic, seismic".')

        parser.add_option(
            '--geodetic_format', dest='geodetic_format',
            type='string', default=['kite'],
            action='callback', callback=list_callback,
            help='Data format to be imported; %s Default: "kite"' %
                 list2string(supported_geodetic_formats))

        parser.add_option(
            '--seismic_format', dest='seismic_format',
            type='string', default='autokiwi',
            help='Data format to be imported; "autokiwi", ...,'
                 'Default: "autokiwi"')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--force', dest='force', action='store_true',
            help='Overwrite existing files')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    c = config.load_config(project_dir, options.mode)

    if not options.results:
        if 'seismic' in options.datatypes:
            sc = c.seismic_config
            logger.info('Attempting to import seismic data from %s' %
                        sc.datadir)

            seismic_outpath = os.path.join(
                c.project_dir, config.seismic_data_name)
            if not os.path.exists(seismic_outpath) or options.force:

                if options.seismic_format == 'autokiwi':

                    stations = model.load_stations(
                        os.path.join(sc.datadir, 'stations.txt'))

                    data_traces = inputf.load_data_traces(
                        datadir=sc.datadir,
                        stations=stations,
                        channels=sc.get_unique_channels())

                    logger.info('Pickle seismic data to %s' % seismic_outpath)
                    utility.dump_objects(seismic_outpath,
                                         outlist=[stations, data_traces])
                else:
                    raise TypeError(
                        'Format: %s not implemented yet.' %
                        options.seismic_format)
            else:
                logger.info('%s exists! Use --force to overwrite!' %
                            seismic_outpath)

        if 'geodetic' in options.datatypes:
            gc = c.geodetic_config
            logger.info('Attempting to import geodetic data from %s' %
                        gc.datadir)

            geodetic_outpath = os.path.join(
                c.project_dir, config.geodetic_data_name)
            if not os.path.exists(geodetic_outpath) or options.force:

                gtargets = []
                for typ in gc.types:
                    if typ == 'SAR':
                        if 'matlab' in options.geodetic_format:
                            gtargets.extend(
                                inputf.load_SAR_data(gc.datadir, gc.names))
                        elif 'kite' in options.geodetic_format:
                            gtargets.extend(
                                inputf.load_kite_scenes(gc.datadir, gc.names))
                        else:
                            raise ImportError(
                                'Format %s not implemented yet for SAR data.' %
                                options.geodetic_format)

                    elif typ == 'GPS':
                        if 'ascii' in options.geodetic_format:
                            for name in gc.names:
                                gtargets.extend(
                                    inputf.load_and_blacklist_GPS(
                                        gc.datadir, name, gc.blacklist))
                        else:
                            raise ImportError(
                                'Format %s not implemented yet for GPS data.' %
                                options.geodetic_format)

                logger.info('Pickleing geodetic data to %s' % geodetic_outpath)
                utility.dump_objects(geodetic_outpath, outlist=gtargets)
            else:
                logger.info('%s exists! Use --force to overwrite!' %
                            geodetic_outpath)

    else:
        if options.mode == 'geometry':
            logger.warn('No previous modeling results to be imported!')

        elif options.mode == 'ffi':
            logger.info('Importing non-linear modeling results, i.e.'
                        ' maximum likelihood result for source geometry.')
            problem = load_model(
                c.project_dir, 'geometry', hypers=False)

            stage = Stage(homepath=problem.outfolder)
            stage.load_results(
                model=problem.model, stage_number=-1, load='full')

            point = backend.get_result_point(stage, problem.config, 'max')
            n_sources = problem.config.problem_config.n_sources

            source_params = problem.config.problem_config.priors.keys()
            for param in point.keys():
                if param not in source_params:
                    point.pop(param)

            point = utility.adjust_point_units(point)
            source_points = utility.split_point(point)

            reference_sources = config.init_reference_sources(
                source_points, n_sources,
                c.problem_config.source_type, c.problem_config.stf_type)

            c.geodetic_config.gf_config.reference_sources = reference_sources
            config.dump_config(c)
            logger.info('Successfully updated config file!')


def command_update(args):

    command_str = 'update'

    def setup(parser):

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--parameters',
            default=['hypers'], type='string',
            action='callback', callback=list_callback,
            help='Parameters to update; "hypers, hierarchicals". '
                 'Default: ["hypers"]')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    config.load_config(
        project_dir, options.mode, update=options.parameters)


def command_clone(args):

    command_str = 'clone'

    def setup(parser):

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--datatypes',
            default=['geodetic', 'seismic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to clone; "geodetic, seismic".')

        parser.add_option(
            '--source_type', dest='source_type',
            choices=config.source_names,
            default=None,
            help='Source type to replace in config; %s'
                 '. Default: "dont change"' % (
                     '", "'.join(name for name in config.source_names)))

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--copy_data', dest='copy_data',
            action='store_true',
            help='If set, the imported data will be copied into the cloned'
                 ' directory.')

        parser.add_option(
            '--sampler', dest='sampler',
            choices=supported_samplers,
            default=None,
            help='Replace sampling algorithm in config to sample '
                 'the solution space of the general problem; %s.'
                 ' Default: "dont change"' % list2string(supported_samplers))

    parser, options, args = cl_parse(command_str, args, setup=setup)

    if not len(args) == 2:
        parser.print_help()
        sys.exit(1)

    name, cloned_name = args

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])
    cloned_dir = os.path.join(os.path.dirname(project_dir), cloned_name)

    util.ensuredir(cloned_dir)

    for mode in [options.mode]:
        config_fn = os.path.join(project_dir, 'config_' + mode + '.yaml')
        if os.path.exists(config_fn):
            logger.info('Cloning %s problem config.' % mode)
            c = config.load_config(project_dir, mode)
            c.name = cloned_name
            c.project_dir = cloned_dir

            new_datatypes = []
            for datatype in options.datatypes:
                if datatype not in c.problem_config.datatypes:
                    logger.warn('Datatype %s to be cloned is not'
                                ' in config!' % datatype)
                else:
                    new_datatypes.append(datatype)

                    data_path = os.path.join(
                        project_dir, datatype + '_data.pkl')

                    if os.path.exists(data_path) and options.copy_data:
                        logger.info('Cloning %s data.' % datatype)
                        cloned_data_path = os.path.join(
                            cloned_dir, datatype + '_data.pkl')
                        shutil.copyfile(data_path, cloned_data_path)

            if options.source_type is None:
                old_priors = copy.deepcopy(c.problem_config.priors)

                c.problem_config.datatype = new_datatypes
                new_priors = c.problem_config.select_variables()
                for prior in new_priors:
                    if prior in old_priors.keys():
                        c.problem_config.priors[prior] = old_priors[prior]

            else:
                logger.info('Replacing source with "%s"' % options.source_type)
                c.problem_config.source_type = options.source_type
                c.problem_config.init_vars()
                c.problem_config.set_decimation_factor()

            old_hypers = copy.deepcopy(c.problem_config.hyperparameters)

            c.update_hypers()
            for hyper in old_hypers.keys():
                c.problem_config.hyperparameters[hyper] = old_hypers[hyper]

            if options.sampler:
                c.sampler_config.name = options.sampler
                c.sampler_config.set_parameters()

            c.regularize()
            c.validate()
            config.dump_config(c)

        else:
            raise IOError('Config file: %s does not exist!' % config_fn)


def command_sample(args):

    command_str = 'sample'

    def setup(parser):
        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--hypers', dest='hypers',
            action='store_true', help='Sample hyperparameters only.')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    problem = load_model(
        project_dir, options.mode, options.hypers)

    step = problem.init_sampler(hypers=options.hypers)

    if options.hypers:
        estimate_hypers(step, problem)
    else:
        sample(step, problem)


def command_summarize(args):

    from pymc3 import summary
    from pymc3.backends.base import MultiTrace

    command_str = 'summarize'

    def setup(parser):

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--force', dest='force', action='store_true',
            help='Overwrite existing files')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    logger.info('Loading problem ...')
    problem = load_model(project_dir, options.mode)

    stage = Stage(homepath=problem.outfolder)
    stage.load_results(model=problem.model, stage_number=-1, load='full')
    stage_path = stage.handler.stage_path(-2)

    if problem.config.sampler_config.name == 'SMC':
        sc_params = problem.config.sampler_config.parameters
        if not os.path.exists(
                os.path.join(stage_path, 'chain-0.csv')) or options.force:

            rtrace = TextChain(stage_path, model=problem.model)
            rtrace.setup(
                draws=sc_params.n_chains * sc_params.n_steps, chain=0)

            if hasattr(problem, 'sources'):
                source = problem.sources[0]
            else:
                source = None

            for chain in stage.mtrace.chains:
                for idx in [-1]:
                    point = stage.mtrace.point(idx=idx, chain=chain)

                    if isinstance(source, MTSourceWithMagnitude):
                        sc = problem.composites['seismic']
                        sc.point2sources(point)
                        ldicts = []
                        for source in sc.sources:
                            ldicts.append(source.scaled_m6_dict)

                        jpoint = utility.join_points(ldicts)
                        point.update(jpoint)

                    lpoint = stage.step['lij'].d2l(point)
                    rtrace.record(lpoint, draw=chain)

            rtrace = MultiTrace([rtrace])
        else:
            logger.info(
                'Summarized trace exists! Use force=True to overwrite!')
            stage.load_results(model=problem.model, stage_number=-2)
            rtrace = stage.mtrace
    else:
        raise NotImplementedError(
            'Summarize function still needs to be implemented '
            'for %s sampler' % problem.config.sampler_config.name)

    if len(rtrace) == 0:
        raise ValueError(
            'Trace collection previously failed. Please rerun'
            ' "beat summarize <project_dir> --force!"')

    summary_file = os.path.join(problem.outfolder, config.summary_name)

    if not os.path.exists(summary_file) or options.force:
        logger.info('Writing summary to %s' % summary_file)
        df = summary(rtrace)
        with open(summary_file, 'w') as outfile:
            df.to_string(outfile)
    else:
        logger.info('Summary exists! Use force=True to overwrite!')


def command_build_gfs(args):

    command_str = 'build_gfs'

    def setup(parser):

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--datatypes',
            default=['geodetic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to calculate the GFs for; "geodetic, seismic".'
                 ' Default: "geodetic"')

        parser.add_option(
            '--force', dest='force', action='store_true',
            help='Overwrite existing files')

        parser.add_option(
            '--execute', dest='execute', action='store_true',
            help='Start actual GF calculations. If not set only'
                 ' configuration files are being created')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    c = config.load_config(project_dir, options.mode)

    if options.mode in ['geometry', 'interseismic']:
        for datatype in options.datatypes:
            if datatype == 'geodetic':
                gc = c.geodetic_config
                gf = c.geodetic_config.gf_config

                for crust_ind in range(*gf.n_variations):
                    heart.geo_construct_gf(
                        event=c.event,
                        geodetic_config=gc,
                        crust_ind=crust_ind,
                        execute=options.execute,
                        force=options.force)

            elif datatype == 'seismic':
                sc = c.seismic_config
                sf = sc.gf_config

                if sf.reference_location is None:
                    logger.info("Creating Green's Function stores individually"
                                " for each station!")
                    seismic_data_path = os.path.join(
                        c.project_dir, config.seismic_data_name)

                    stations, _ = utility.load_objects(seismic_data_path)
                    stations = utility.apply_station_blacklist(
                        stations, sc.blacklist)
                    stations = utility.weed_stations(
                        stations, c.event, distances=sc.distances)
                else:
                    logger.info(
                        "Creating one global Green's Function store, which is "
                        "being used by all stations!")
                    stations = [sf.reference_location]
                    logger.info(
                        'Store name: %s' % sf.reference_location.station)

                for crust_ind in range(*sf.n_variations):
                    heart.seis_construct_gf(
                        stations=stations,
                        event=c.event,
                        seismic_config=sc,
                        crust_ind=crust_ind,
                        execute=options.execute,
                        force=options.force)
            else:
                raise ValueError('Datatype %s not supported!' % datatype)

            if not options.execute:
                logger.info('%s GF store configs successfully created! '
                            'To start calculations set --execute!' % datatype)

            if options.execute:
                logger.info('%s GF calculations successful!' % datatype)

    elif options.mode == 'ffi':
        from beat import ffi

        slip_varnames = c.problem_config.get_slip_variables()
        varnames = c.problem_config.select_variables()
        outdir = os.path.join(
            c.project_dir, options.mode, config.linear_gf_dir_name)
        util.ensuredir(outdir)

        faultpath = os.path.join(outdir, config.fault_geometry_name)
        if not os.path.exists(faultpath) or options.force:
            for datatype in options.datatypes:
                try:
                    gf = c[datatype + '_config'].gf_config
                except AttributeError:
                    raise AttributeError(
                        'Datatype "%s" not existing in config!' % datatype)

                for source in gf.reference_sources:
                    source.update(lat=c.event.lat, lon=c.event.lon)

                logger.info('Discretizing reference sources ...')
                fault = ffi.discretize_sources(
                    varnames=slip_varnames,
                    sources=gf.reference_sources,
                    extension_width=gf.extension_width,
                    extension_length=gf.extension_length,
                    patch_width=gf.patch_width,
                    patch_length=gf.patch_length,
                    datatypes=options.datatypes)

            logger.info(
                'Storing discretized fault geometry to: %s' % faultpath)
            utility.dump_objects(faultpath, [fault])

            if c.problem_config.n_sources != fault.npatches:
                logger.info(
                    'Fault discretization changed! Updating problem_config:')
                logger.info('%s' % fault.__str__())
                c.problem_config.n_sources = fault.npatches
                c.problem_config.init_vars(varnames)

            ext_source = fault.get_subfault(
                0, datatype=options.datatypes[0], component='uparr')

            new_bounds = {
                'nucleation_strike': (0., ext_source.length / km),
                'nucleation_dip': (0., ext_source.width / km)
            }

            c.problem_config.set_vars(new_bounds)
            config.dump_config(c)

        elif os.path.exists(faultpath):
            logger.info("Discretized fault geometry exists! Use --force to"
                        " overwrite!")
            logger.info('Loading existing discretized fault')
            fault = utility.load_objects(faultpath)[0]

        if options.execute:
            logger.info("Calculating linear Green's Functions")

            for datatype in options.datatypes:
                logger.info('for %s data ...' % datatype)

                if datatype == 'geodetic':
                    gf = c.geodetic_config.gf_config

                    geodetic_data_path = os.path.join(
                        c.project_dir, config.geodetic_data_name)

                    datasets = utility.load_objects(geodetic_data_path)

                    engine = LocalEngine(store_superdirs=[gf.store_superdir])

                    for crust_ind in range(*gf.n_variations):
                        logger.info('crust_ind %i' % crust_ind)

                        targets = heart.init_geodetic_targets(
                            datasets,
                            earth_model_name=gf.earth_model_name,
                            interpolation=c.geodetic_config.interpolation,
                            crust_inds=[crust_ind],
                            sample_rate=gf.sample_rate)

                        ffi.geo_construct_gf_linear(
                            engine=engine,
                            outdirectory=outdir,
                            event=c.event,
                            crust_ind=crust_ind,
                            datasets=datasets,
                            targets=targets,
                            nworkers=gf.nworkers,
                            fault=fault,
                            varnames=slip_varnames,
                            force=options.force)

                elif datatype == 'seismic':
                    seismic_data_path = os.path.join(
                        c.project_dir, config.seismic_data_name)
                    sc = c.seismic_config
                    gf = sc.gf_config
                    pc = c.problem_config

                    engine = LocalEngine(store_superdirs=[gf.store_superdir])

                    for crust_ind in range(*gf.n_variations):
                        logger.info('crust_ind %i' % crust_ind)
                        sc.gf_config.reference_model_idx = crust_ind
                        datahandler = heart.init_datahandler(
                            seismic_config=sc,
                            seismic_data_path=seismic_data_path)

                        for wc in sc.waveforms:
                            wmap = heart.init_wavemap(
                                waveformfit_config=wc,
                                datahandler=datahandler,
                                event=c.event)

                            ffi.seis_construct_gf_linear(
                                engine=engine,
                                fault=fault,
                                durations_prior=pc.priors['durations'],
                                velocities_prior=pc.priors['velocities'],
                                varnames=slip_varnames,
                                wavemap=wmap,
                                event=c.event,
                                nworkers=gf.nworkers,
                                starttime_sampling=gf.starttime_sampling,
                                duration_sampling=gf.duration_sampling,
                                sample_rate=gf.sample_rate,
                                outdirectory=outdir,
                                force=options.force)
        else:
            logger.info('Did not run GF calculation. Use --execute!')


def command_plot(args):

    command_str = 'plot'
    from beat import plotting
    
    def setup(parser):

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--mode',
            dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--post_llk',
            dest='post_llk',
            choices=['max', 'min', 'mean', 'all'],
            default='max',
            help='Plot model with specified likelihood; "max", "min", "mean"'
                 ' or "all"; Default: "max"')

        parser.add_option(
            '--stage_number',
            dest='stage_number',
            type='int',
            default=None,
            help='Int of the stage number "n" of the stage to be plotted.'
                 ' Default: all stages up to last complete stage')

        parser.add_option(
            '--varnames',
            default='',
            type='string',
            action='callback', callback=list_callback,
            help='Variable names to plot in figures. Example: "strike,dip"'
                 ' Default: empty string --> all')

        parser.add_option(
            '--format',
            dest='format',
            choices=['display', 'pdf', 'png', 'svg', 'eps'],
            default='pdf',
            help='Output format of the plot; "display", "pdf", "png", "svg",'
                 '"eps" Default: "pdf"')

        parser.add_option(
            '--plot_projection',
            dest='plot_projection',
            choices=['latlon', 'local'],
            default='local',
            help='Output projection of the plot; "latlon" or "local"'
                 'Default: "local"')

        parser.add_option(
            '--dpi',
            dest='dpi',
            type='int',
            default=300,
            help='Output resolution of the plots in dpi (dots per inch);'
                 ' Default: "300"')

        parser.add_option(
            '--force',
            dest='force',
            action='store_true',
            help='Overwrite existing files')

        parser.add_option(
            '--reference',
            dest='reference',
            action='store_true',
            help='Plot reference (test_point) into stage posteriors.')

        parser.add_option(
            '--hypers',
            dest='hypers',
            action='store_true',
            help='Plot hyperparameter results only.')

        parser.add_option(
            '--nobuild',
            dest='nobuild',
            action='store_true',
            help='Dont build models during problem init.')

    plots_avail = plotting.available_plots()

    details = '''Available <plot types> are: %s or "all". Multiple plots can be
selected giving a comma seperated list.''' % list2string(plots_avail)

    parser, options, args = cl_parse(command_str, args, setup, details)

    if len(args) < 1:
        parser.error('plot needs at least one argument!')
        parser.help()

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str], popflag=True)

    if args[0] == 'all':
        plotnames = plots_avail
    else:
        plotnames = args[0].split(',')

    for plot in plotnames:
        if plot not in plots_avail:
            raise TypeError('Plot type %s not available! Available plots are:'
                            ' %s' % (plot, plots_avail))

    logger.info('Loading problem ...')
    problem = load_model(
        project_dir, options.mode, options.hypers, options.nobuild)

    po = plotting.PlotOptions(
        plot_projection=options.plot_projection,
        post_llk=options.post_llk,
        load_stage=options.stage_number,
        outformat=options.format,
        force=options.force,
        dpi=options.dpi,
        varnames=options.varnames)

    if options.reference:
        po.reference = problem.model.test_point
    else:
        po.reference = None

    figure_path = os.path.join(problem.outfolder, po.figure_dir)
    util.ensuredir(figure_path)

    for plot in plotnames:
        plotting.plots_catalog[plot](problem, po)


def command_check(args):

    command_str = 'check'

    def setup(parser):
        parser.add_option(
            '--mode',
            dest='mode',
            choices=mode_choices,
            default='geometry',
            help='Inversion problem to solve; %s Default: "geometry"' %
                 list2string(mode_choices))

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--datatypes',
            default=[' seismic'],
            type='string',
            action='callback',
            callback=list_callback,
            help='Datatypes to check; "geodetic, seismic".')

        parser.add_option(
            '--what',
            dest='what',
            choices=['stores', 'traces', 'library'],
            default='stores',
            help='Setup item to check; "stores, traces", Default: "stores"')

        parser.add_option(
            '--targets',
            default=[0],
            type='string',
            action='callback',
            callback=list_callback,
            help='Indexes to targets to display.')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    problem = load_model(
        project_dir, options.mode, hypers=False, nobuild=True)

    if options.what == 'stores':
        corrupted_stores = heart.check_problem_stores(
            problem, options.datatypes)

        for datatype in options.datatypes:
            store_ids = corrupted_stores[datatype]
            logger.warn('Store(s) with empty traces! : %s ' % store_ids)

    elif options.what == 'traces':
        sc = problem.composites['seismic']
        for wmap in sc.wavemaps:
            snuffle(
                wmap.datasets, stations=wmap.stations, events=[sc.event])

    elif options.what == 'library':
        if options.mode != 'ffi':
            logger.warning(
                'GF library exists only for "ffi" optimization mode.')
        else:
            from beat import ffi

            for datatype in options.datatypes:
                for var in problem.config.problem_config.get_slip_variables():
                    outdir = os.path.join(
                        problem.config.project_dir, options.mode,
                        config.linear_gf_dir_name)
                    if datatype == 'seismic':
                        sc = problem.config.seismic_config
                        scomp = problem.composites['seismic']

                        for wmap in scomp.wavemaps:
                            filename = ffi.get_gf_prefix(
                                datatype, component=var,
                                wavename=wmap.config.name,
                                crust_ind=sc.gf_config.reference_model_idx)

                            logger.info(
                                'Loading Greens Functions'
                                ' Library %s for %s target' % (
                                    filename,
                                    utility.list2string(options.targets)))
                            gfs = ffi.load_gf_library(
                                directory=outdir, filename=filename)

                            targets = [
                                int(target) for target in options.targets]
                            trs = gfs.get_traces(
                                targetidxs=targets,
                                patchidxs=range(gfs.npatches),
                                durationidxs=range(gfs.ndurations),
                                starttimeidxs=range(gfs.nstarttimes))
                            snuffle(trs)
    else:
        raise ValueError('Subject what: %s is not available!' % options.what)


def main():

    if len(sys.argv) < 2:
        sys.exit('Usage: %s' % usage)

    args = list(sys.argv)
    args.pop(0)
    command = args.pop(0)

    if command in subcommands:
        globals()['command_' + command](args)

    elif command in ('--help', '-h', 'help'):
        if command == 'help' and args:
            acommand = args[0]
            if acommand in subcommands:
                globals()['command_' + acommand](['--help'])

        sys.exit('Usage: %s' % usage)

    else:
        sys.exit('BEAT: error: no such subcommand: %s' % command)


if __name__ == '__main__':
    main()
