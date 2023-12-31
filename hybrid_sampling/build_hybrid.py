""" Script to build a hybrid sampling bilby job. """
import os
import sys

import bilby_pipe.main
from bilby.core.prior import PriorDict
from bilby_pipe.job_creation.bilby_pipe_dag_creator import get_trigger_time_list
from bilby_pipe.parser import create_parser
from bilby_pipe.utils import (
    get_colored_string,
    get_command_line_arguments,
    get_outdir_name,
    logger,
    nonestr,
    parse_args,
)


def generate_hybrid_submit(args):
    """

    Replace the default HTCondor `.submit` file for postprocessing generated by
    bilby with one that launches a parameter estimation job for each
    post-Newtonian deivation parameter.

    Parameters
    ==========
    args: argparse.Namespace
        Command-line arguments / arguments passed in a bilby `.ini` file, ingested
        by an argument parser created by `bilby_pipe.parser.create_parser()`.

    Returns
    =======
    None

    """

    # get the parent directory of the dynesty run,
    # and the `name` of the outdir, i.e. directory of the dynesty run without
    # leading path information
    outdir = os.path.abspath(args.outdir)
    initialdir, name = os.path.split(outdir)
    logdir = f"{name}/log_data_analysis"

    # set paths for the initialization and prior distributions for the deviations
    initdir = (
        initialdir
        if args.hybrid_seed_priors_dir is None
        else os.path.abspath(args.hybrid_seed_priors_dir)
    )
    newdir = (
        initialdir
        if args.hybrid_priors_dir is None
        else os.path.abspath(args.hybrid_priors_dir)
    )

    # label for hybrid runs; $(PN_COEFF) is expanded by HTCondor to be
    # d_phi_i, d_alpha_i, or d_beta_i for the post-Newtonian deviation being inferred
    hybrid_extra_label = "$(PN_COEFF)_overlap-$(OVERLAP_CUT)"
    if args.hybrid_label is not None:
        hybrid_extra_label += f"_{args.hybrid_label}"

    ifo_str = "".join(args.detectors)

    trigger_times = get_trigger_time_list(args)
    # if we only have one trigger time but many simulations at that time,
    # duplicate it so the next loop works!
    if len(trigger_times) == 1 and args.n_simulation > 1:
        trigger_times = [trigger_times[0] for i in range(args.n_simulation)]

    for idx, trigger_time in enumerate(trigger_times):
        if trigger_time is None:
            trigger_time_str = "0-0"
        else:
            trigger_time_str = "-".join(str(float(trigger_time)).split("."))

        bilby_longname = f"{args.label}_data{idx}_{trigger_time_str}_analysis_{ifo_str}"
        postprocess_name = f"{bilby_longname}_postprocess_single_{hybrid_extra_label}"

        logpath = os.path.join(logdir, f"{postprocess_name}.log")
        errpath = os.path.join(logdir, f"{postprocess_name}.err")
        outpath = os.path.join(logdir, f"{postprocess_name}.out")

        initial_resultpath = os.path.join(
            name, "result", f"{bilby_longname}_result.json"
        )
        extra_prior_path = os.path.join(newdir, "new-$(PN_COEFF).prior")
        extra_init_path = os.path.join(initdir, "init-$(PN_COEFF).prior")

        # setup path to file with all of the dpi we want to run
        if args.hybrid_runs is None:
            queue_path = os.path.join(initialdir, "queue.txt")
        else:
            queue_path = os.path.abspath(args.hybrid_runs)

        # check that, for each dpi we want to run, there is a valid bilby
        # initialization and new prior!
        prior_exception = None
        with open(queue_path, "r") as queue_file:
            for line in queue_file.readlines():
                try:
                    dpi = line.split(",")[0].strip()
                except Exception as ex:
                    logger.error(
                        get_colored_string(
                            f"Error reading --hybrid-runs file at {queue_path}! (Format may be wrong)."
                        )
                    )
                    raise ex

                # try to load the initialization and new priors for each dpi
                init_prior_path = os.path.join(initdir, f"init-{dpi}.prior")
                new_prior_path = os.path.join(newdir, f"new-{dpi}.prior")

                try:
                    PriorDict(init_prior_path)
                except FileNotFoundError as fnf:
                    logger.error(
                        get_colored_string(
                            f"Seed prior for {dpi} not found at {init_prior_path}!"
                        )
                    )
                    prior_exception = fnf
                except Exception as ex:
                    logger.error(
                        get_colored_string(
                            f"Seed prior for {dpi} at {init_prior_path} not understood, or another error occurred."
                        )
                    )
                    prior_exception = ex

                try:
                    PriorDict(new_prior_path)
                except FileNotFoundError as fnf:
                    logger.error(
                        get_colored_string(
                            f"Prior for {dpi} not found at {new_prior_path}!"
                        )
                    )
                    prior_exception = fnf
                except Exception as ex:
                    logger.error(
                        get_colored_string(
                            f"Prior for {dpi} at {new_prior_path} not understood, or another error occurred."
                        )
                    )
                    prior_exception = ex

        if prior_exception is not None:
            raise prior_exception

        # grab the appropriate python executable path
        # TODO: make this more general
        conda_bin = os.path.split(sys.executable)[0]
        executable_path = f"{conda_bin}/{args.single_postprocessing_executable}"
        if not os.path.isfile:
            err_msg = f"No python executable found; no file found at {executable_path}."
            logger.error(get_colored_string(err_msg))
            raise FileNotFoundError(err_msg)
        elif not os.access(executable_path, os.X_OK):
            err_msg = f"No python executable found; the file found at {executable_path} \
            isn't executable, or you lack the permissions to execute it."
            logger.error(get_colored_string(err_msg))
            raise ValueError(err_msg)

        # construct dictionary of condor submit arguments
        arg_dict = dict(
            universe="vanilla",
            executable=executable_path,
            request_memory="4 GB",
            request_cpus=1,
            request_disk="5 MB",
            getenv=True,
            initialdir=initialdir,
            notification="Never",
            requirements="\n+SuccessCheckpointExitCode = 77\n+WantFTOnCheckpoint = True",
            log=logpath,
            output=outpath,
            error=errpath,
            accounting_group=args.accounting,
            priority=0,
            arguments=f"\"{initial_resultpath} \
                --extra-prior-file {extra_prior_path} \
                --extra-initialization-prior {extra_init_path} \
                --extra-label {hybrid_extra_label} \
                --waveform-arguments '{{''min_match'': $(OVERLAP_CUT) }}'\"",
        )

        hybrid_submit_name = f"{bilby_longname}_postprocess_single.submit"
        hybrid_submit_path = os.path.join(outdir, "submit", hybrid_submit_name)

        # overwrite the bilby postprocess .submit file with our new configuration
        with open(hybrid_submit_path, "w") as hybrid_submit_file:
            for key, val in arg_dict.items():
                hybrid_submit_file.write("%s = %s\n" % (key, str(val)))

            hybrid_submit_file.write(f"queue PN_COEFF, OVERLAP_CUT from {queue_path}")


def main():
    """

    Main program when called as an executable (see `../setup.py`).
    Creates an argument parser with `bilby_pipe.parser.create_parser()`, and then
    adds additional arguments for generation of the hybrid post-processing step.

    The job is built and launched by bilby_pipe as normal, using `bilby_pipe.main.main()`.
    This includes launching the job, if given the `--submit` flag. In addition,
    however, we modify the postprocessing `.submit` file so that it will launch
    parameter estimation jobs for all of the desired post-Newtonian deviation
    parameters.

    """

    # create bilby_pipe parser
    parser = create_parser()

    # add arguments for the hybrid postprocessing step
    hybrid_parser = parser.add_argument_group(
        "Arguments for the hybrid parser.",
    )

    hybrid_parser.add_argument(
        "--hybrid-seed-priors-dir",
        type=nonestr,
        default=None,
        help="""Directory with files defining distributions from which the
        post-Newtonian deviations are initialized in the second-step of
        hybrid sampling. Files should be named as `init-{d_phi_i}.prior`.
        Defaults to the parent directory of the provided outdir.""",
    )
    hybrid_parser.add_argument(
        "--hybrid-priors-dir",
        type=nonestr,
        default=None,
        help="""Directory with files defining prior distributions for the
        post-Newtonian deviations in the second-stpe of hybrid sampling. Files
        should be named as `new-{d_phi_i}.prior`.
        Defaults to the parent directory of the provided outdir.""",
    )
    hybrid_parser.add_argument(
        "--hybrid-runs",
        type=nonestr,
        default=None,
        help="""Path to an HTCondor queue file specifying the post-Newtonian
        deviations to be varied, and the overlap cut to apply to the prior of each.
        For each post-Newtonian deviation you want to infer, include a line in this
        file that is `d_pi, overlap_cut`, where `overlap_cut` is the cut on the
        prior of that deviation parameter.
        Defaults to a file named `queue.txt` in the parent directory of the
        provided outdir.""",
    )
    hybrid_parser.add_argument(
        "--hybrid-label",
        type=nonestr,
        default=None,
        help="""A custom label to append to the second-step hybrid sampling
        log and output files. This is appended to an existing label defining the
        post-Newtonian deviation being varied and the overlap cut applied,
        which is itself appended to the label provided to bilby_pipe.""",
    )

    args, _ = parse_args(get_command_line_arguments(), parser)

    # We get the outdir before running bilby_pipe.main.main, as that method will
    # also check if the outdir exists, and then create it-- so if we ran this
    # second, it would increment the name once more
    if not args.overwrite_outdir:
        outdir = get_outdir_name(os.path.abspath(args.outdir))
        args.outdir = outdir

    # build bilby run from provided args
    bilby_pipe.main.main()

    # write a nice submit file for all of the post-processing, replacing
    # that which is generated by bilby_pipe
    generate_hybrid_submit(args)
