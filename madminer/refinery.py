from __future__ import absolute_import, division, print_function, unicode_literals

import logging
import numpy as np
import collections
import six

from madminer.tools.h5_interface import load_madminer_settings, madminer_event_loader, save_events_to_madminer_file
from madminer.tools.analysis import get_theta_value, get_theta_benchmark_matrix, get_dtheta_benchmark_matrix
from madminer.tools.analysis import extract_augmented_data, parse_theta
from madminer.tools.morphing import Morpher
from madminer.tools.utils import general_init, format_benchmark, create_missing_folders, shuffle, balance_thetas


def combine_and_shuffle(input_filenames,
                        output_filename,
                        overwrite_existing_file=True,
                        debug=False):
    """
    Combines multiple HDF5 files into one, and shuffles the order of the events. It is recommended to run this tool
    before the Refinery.

    :param input_filenames: list of filenames of the input HDF5 files
    :param output_filename: filename for the output HDF5 file
    :param overwrite_existing_file:
    :param debug:
    """

    general_init(debug=debug)

    if len(input_filenames) > 1:
        logging.warning('Careful: this tool assumes that all samples are generated with the same setup, including'
                        ' identical benchmarks (and thus morphing setup). If it is used with samples with different'
                        ' settings, there will be wrong results! There are no explicit cross checks in place yet.')

    # Copy first file to output_filename
    logging.info('Copying setup from %s to %s', input_filenames[0], output_filename)

    # TODO: More memory efficient strategy

    # Load events
    all_observations = None
    all_weights = None

    for i, filename in enumerate(input_filenames):
        logging.info('Loading samples from file %s / %s at %s', i + 1, len(input_filenames), filename)

        for observations, weights in madminer_event_loader(filename):
            if all_observations is None:
                all_observations = observations
                all_weights = weights
            else:
                all_observations = np.vstack((all_observations, observations))
                all_weights = np.vstack((all_weights, weights))

    # Shuffle
    all_observations, all_weights = shuffle(all_observations, all_weights)

    # Save result
    save_events_to_madminer_file(
        filename=output_filename,
        observations=all_observations,
        weights=all_weights,
        copy_setup_from=input_filenames[0],
        overwrite_existing_samples=overwrite_existing_file
    )


def constant_benchmark_theta(benchmark_name):
    return 'benchmark', benchmark_name


def multiple_benchmark_thetas(benchmark_names):
    return 'benchmarks', benchmark_names


def constant_morphing_theta(theta):
    return 'theta', np.asarray(theta)


def multiple_morphing_thetas(thetas):
    return 'thetas', [np.asarray(theta) for theta in thetas]


def random_morphing_thetas(n_thetas, priors):
    return 'random', (n_thetas, priors)


class Refinery:

    def __init__(self, filename, disable_morphing=False, debug=False):

        general_init(debug=debug)

        self.madminer_filename = filename

        logging.info('Loading data from %s', filename)

        # Load data
        (self.parameters, self.benchmarks, self.morphing_components, self.morphing_matrix,
         self.observables, self.n_samples) = load_madminer_settings(filename)

        self.n_parameters = len(self.parameters)

        logging.info('Found %s parameters:', self.n_parameters)
        for key, values in six.iteritems(self.parameters):
            logging.info('   %s (LHA: %s %s, maximal power in squared ME: %s, range: %s)',
                         key, values[0], values[1], values[2], values[3])

        logging.info('Found %s benchmarks:', len(self.benchmarks))
        for key, values in six.iteritems(self.benchmarks):
            logging.info('   %s: %s',
                         key, format_benchmark(values))

        logging.info('Found %s observables: %s', len(self.observables), ', '.join(self.observables))
        logging.info('Found %s events', self.n_samples)

        # Morphing
        self.morpher = None
        if self.morphing_matrix is not None and self.morphing_components is not None and not disable_morphing:
            self.morpher = Morpher(self.parameters)
            self.morpher.set_components(self.morphing_components)
            self.morpher.set_basis(self.benchmarks, morphing_matrix=self.morphing_matrix)

            logging.info('Found morphing setup with %s components', len(self.morphing_components))

        else:
            logging.info('Did not find morphing setup.')

    def extract_samples_train_plain(self,
                                    theta,
                                    n_samples,
                                    folder,
                                    filename,
                                    test_split=0.3):
        """
        Extracts training samples x ~ p(x|theta) for methods such as histograms or ABC.

        :param theta: tuple (type, value) that defines the parameter point or prior over parameter points for the
                      sampling. Use the helper functions constant_benchmark_theta(), multiple_benchmark_thetas(),
                      constant_morphing_theta(), multiple_morphing_thetas(), or random_morphing_thetas().
        :param n_samples: Total number of samples to be drawn.
        :param folder: Folder for the resulting samples.
        :param filename: Label for the filenames. The actual filenames will add a prefix such as 'x_', and the extension
                         '.npy'.
        :param test_split: Fraction of events reserved for the test sample (will not be used for any training samples).
        """

        logging.info('Extracting plain training sample. Sampling according to %s', theta)

        create_missing_folders([folder])

        # Thetas
        theta_types, theta_values, n_samples_per_theta = parse_theta(theta, n_samples)

        # Train / test split
        if test_split is None or test_split <= 0. or test_split >= 1.:
            last_train_index = None
        else:
            last_train_index = int(round((1. - test_split) * self.n_samples, 0))

            if last_train_index < 0 or last_train_index > self.n_samples:
                raise ValueError("Irregular train / test split: sample {} / {}", last_train_index, self.n_samples)

        # Start
        x, _, (theta,) = self.extract_sample(
            theta_sets_types=[theta_types],
            theta_sets_values=[theta_values],
            n_samples_per_theta=n_samples_per_theta,
            start_event=0,
            end_event=last_train_index
        )

        # Save data
        np.save(folder + '/theta_' + filename + '.npy', theta)
        np.save(folder + '/x_' + filename + '.npy', x)

        return x, theta

    def extract_samples_train_local(self,
                                    theta,
                                    n_samples,
                                    folder,
                                    filename,
                                    test_split=0.3):
        """
        Extracts training samples x ~ p(x|theta) as well as the joint score t(x, z|theta) for SALLY and SALLINO.

        :param theta: tuple (type, value) that defines the parameter point or prior over parameter points for the
                      sampling. This is also where the score is evaluated. Use the helper functions, in particular
                      constant_benchmark_theta() and constant_morphing_theta().
        :param n_samples: Total number of samples to be drawn.
        :param folder: Folder for the resulting samples.
        :param filename: Label for the filenames. The actual filenames will add a prefix such as 'x_', and the extension
                         '.npy'.
        :param test_split: Fraction of events reserved for the test sample (will not be used for any training samples).
        """

        logging.info('Extracting training sample for local score regression. Sampling and score evaluation according to'
                     ' %s', theta)

        create_missing_folders([folder])

        if self.morpher is None:
            raise RuntimeError('No morphing setup loaded. Cannot calculate score.')

        # Thetas
        theta_types, theta_values, n_samples_per_theta = parse_theta(theta, n_samples)

        # Augmented data (gold)
        augmented_data_definitions = [('score', 0)]

        # Train / test split
        if test_split is None or test_split <= 0. or test_split >= 1.:
            last_train_index = None
        else:
            last_train_index = int(round((1. - test_split) * self.n_samples, 0))

            if last_train_index < 0 or last_train_index > self.n_samples:
                raise ValueError("Irregular train / test split: sample {} / {}", last_train_index, self.n_samples)

        # Start
        x, (t_xz,), (theta,) = self.extract_sample(
            theta_sets_types=[theta_types],
            theta_sets_values=[theta_values],
            n_samples_per_theta=n_samples_per_theta,
            augmented_data_definitions=augmented_data_definitions,
            start_event=0,
            end_event=last_train_index
        )

        # Save data
        np.save(folder + '/theta_' + filename + '.npy', theta)
        np.save(folder + '/x_' + filename + '.npy', x)
        np.save(folder + '/t_xz_' + filename + '.npy', t_xz)

        return x, theta, t_xz

    def extract_samples_train_ratio(self,
                                    theta0,
                                    theta1,
                                    n_samples,
                                    folder,
                                    filename,
                                    test_split=0.3):
        """
        Extracts training samples x ~ p(x|theta0) and x ~ p(x|theta1) together with the class label y, the joint
        likelihood ratio r(x,z|theta0, theta1), and the joint score t(x,z|theta0) for methods such as CARL, ROLR,
        CASCAL, and RASCAL.

        :param theta0: tuple (type, value) that defines the numerator parameter point or prior over parameter points.
                       Use the helper functions constant_benchmark_theta(), multiple_benchmark_thetas(),
                       constant_morphing_theta(), multiple_morphing_thetas(), or random_morphing_thetas().
        :param theta1: tuple (type, value) that defines the numerator parameter point or prior over parameter points.
                       Use the helper functions constant_benchmark_theta(), multiple_benchmark_thetas(),
                       constant_morphing_theta(), multiple_morphing_thetas(), or random_morphing_thetas().
        :param n_samples: Total number of samples to be drawn.
        :param folder: Folder for the resulting samples.
        :param filename: Label for the filenames. The actual filenames will add a prefix such as 'x_', and the extension
                         '.npy'.
        :param test_split: Fraction of events reserved for the test sample (will not be used for any training samples).
        """

        logging.info('Extracting training sample for ratio-based methods. Numerator hypothesis: %s, denominator '
                     'hypothesis: %s', theta0, theta1)

        if self.morpher is None:
            raise RuntimeError('No morphing setup loaded. Cannot calculate score.')

        create_missing_folders([folder])

        # Augmented data (gold)
        augmented_data_definitions = [('ratio', 0, 1),
                                      ('score', 0)]

        # Train / test split
        if test_split is None or test_split <= 0. or test_split >= 1.:
            last_train_index = None
        else:
            last_train_index = int(round((1. - test_split) * self.n_samples, 0))

            if last_train_index < 0 or last_train_index > self.n_samples:
                raise ValueError("Irregular train / test split: sample {} / {}", last_train_index, self.n_samples)

        # Thetas for theta0 sampling
        theta0_types, theta0_values, n_samples_per_theta0 = parse_theta(theta0, n_samples // 2)
        theta1_types, theta1_values, n_samples_per_theta1 = parse_theta(theta1, n_samples // 2)

        n_samples_per_theta = min(n_samples_per_theta0, n_samples_per_theta1)

        # Start for theta0
        x0, (r_xz0, t_xz0), (theta0_0, theta1_0) = self.extract_sample(
            theta_sets_types=[theta0_types, theta1_types],
            theta_sets_values=[theta0_values, theta1_values],
            sampling_theta_index=0,
            n_samples_per_theta=n_samples_per_theta,
            augmented_data_definitions=augmented_data_definitions,
            start_event=0,
            end_event=last_train_index
        )

        # Thetas for theta1 sampling (could be different if num or denom are random)
        theta0_types, theta0_values, n_samples_per_theta0 = parse_theta(theta0, n_samples // 2)
        theta1_types, theta1_values, n_samples_per_theta1 = parse_theta(theta1, n_samples // 2)

        n_samples_per_theta = min(n_samples_per_theta0, n_samples_per_theta1)

        # Start for theta1
        x1, (r_xz1, t_xz1), (theta0_1, theta1_1) = self.extract_sample(
            theta_sets_types=[theta0_types, theta1_types],
            theta_sets_values=[theta0_values, theta1_values],
            sampling_theta_index=1,
            n_samples_per_theta=n_samples_per_theta,
            augmented_data_definitions=augmented_data_definitions,
            start_event=0,
            end_event=last_train_index
        )

        # Combine
        x = np.vstack([x0, x1])
        r_xz = np.vstack([r_xz0, r_xz1])
        t_xz = np.vstack([t_xz0, t_xz1])
        theta0 = np.vstack([theta0_0, theta0_1])
        theta1 = np.vstack([theta1_0, theta1_1])
        y = np.zeros(x.shape[0])
        y[x0.shape[0]:] = 1.

        # Shuffle
        x, r_xz, t_xz, theta0, theta1, y = shuffle(x, r_xz, t_xz, theta0, theta1, y)

        # y shape
        y = y.reshape((-1, 1))

        # Save data
        np.save(folder + '/theta0_' + filename + '.npy', theta0)
        np.save(folder + '/theta1_' + filename + '.npy', theta1)
        np.save(folder + '/x_' + filename + '.npy', x)
        np.save(folder + '/y_' + filename + '.npy', y)
        np.save(folder + '/r_xz_' + filename + '.npy', r_xz)
        np.save(folder + '/t_xz_' + filename + '.npy', t_xz)

        return x, theta0, theta1, y, r_xz, t_xz

    def extract_samples_train_more_ratios(self,
                                          theta0,
                                          theta1,
                                          n_samples,
                                          folder,
                                          filename,
                                          additional_thetas=None,
                                          test_split=0.3):
        """
        Extracts training samples x ~ p(x|theta0) and x ~ p(x|theta1) together with the class label y, the joint
        likelihood ratio r(x,z|theta0, theta1), and the joint scores t(x,z|theta0) as well as t(x,z|theta1) for methods
        such as CARL, ROLR, CASCAL, and RASCAL.

        :param theta0: tuple (type, value) that defines the numerator parameter point or prior over parameter points.
                       Use the helper functions constant_benchmark_theta(), multiple_benchmark_thetas(),
                       constant_morphing_theta(), multiple_morphing_thetas(), or random_morphing_thetas().
        :param theta1: tuple (type, value) that defines the numerator parameter point or prior over parameter points.
                       Use the helper functions constant_benchmark_theta(), multiple_benchmark_thetas(),
                       constant_morphing_theta(), multiple_morphing_thetas(), or random_morphing_thetas().
        :param n_samples: Total number of samples to be drawn.
        :param folder: Folder for the resulting samples.
        :param filename: Label for the filenames. The actual filenames will add a prefix such as 'x_', and the extension
                         '.npy'.
        :param additional_thetas: list of tuples (type, value) that defines additional theta points at which ratio and
                                  score are evaluated, and which are then used to create additional training data
                                  points. Use the helper functions constant_benchmark_theta(),
                                  multiple_benchmark_thetas(), constant_morphing_theta(), multiple_morphing_thetas(), or
                                  random_morphing_thetas().
        :param test_split: Fraction of events reserved for the test sample (will not be used for any training samples).
        """

        logging.info('Extracting training sample for ratio-based methods. Numerator hypothesis: %s, denominator '
                     'hypothesis: %s', theta0, theta1)

        if self.morpher is None:
            raise RuntimeError('No morphing setup loaded. Cannot calculate score.')

        create_missing_folders([folder])

        if additional_thetas is None:
            additional_thetas = []
        n_additional_thetas = len(additional_thetas)

        # Augmented data (gold)
        augmented_data_definitions_0 = [('ratio', 0, 1),
                                        ('score', 0),
                                        ('score', 1)]
        augmented_data_definitions_1 = [('ratio', 0, 1),
                                        ('score', 0),
                                        ('score', 1)]
        for i in range(n_additional_thetas):
            augmented_data_definitions_0.append(('ratio', 0, i + 2))
            augmented_data_definitions_0.append(('score', i + 2))
            augmented_data_definitions_1.append(('ratio', i + 2, 1))
            augmented_data_definitions_1.append(('score', i + 2))

        # Train / test split
        if test_split is None or test_split <= 0. or test_split >= 1.:
            last_train_index = None
        else:
            last_train_index = int(round((1. - test_split) * self.n_samples, 0))

            if last_train_index < 0 or last_train_index > self.n_samples:
                raise ValueError("Irregular train / test split: sample {} / {}", last_train_index, self.n_samples)

        # Parse thetas for theta0 sampling
        theta_types = []
        theta_values = []
        n_samples_per_theta = 1000000

        theta0_types, theta0_values, this_n_samples = parse_theta(theta0, n_samples // 2)
        theta_types.append(theta0_types)
        theta_values.append(theta0_values)
        n_samples_per_theta = min(this_n_samples, n_samples_per_theta)

        theta1_types, theta1_values, this_n_samples = parse_theta(theta1, n_samples // 2)
        theta_types.append(theta1_types)
        theta_values.append(theta1_values)
        n_samples_per_theta = min(this_n_samples, n_samples_per_theta)

        for additional_theta in additional_thetas:
            additional_theta_types, additional_theta_values, this_n_samples = parse_theta(additional_theta,
                                                                                          n_samples // 2)
            theta_types.append(additional_theta_types)
            theta_values.append(additional_theta_values)
            n_samples_per_theta = min(this_n_samples, n_samples_per_theta)

        # Start for theta0
        x_0, augmented_data_0, thetas_0 = self.extract_sample(
            theta_sets_types=theta_types,
            theta_sets_values=theta_values,
            n_samples_per_theta=n_samples_per_theta,
            augmented_data_definitions=augmented_data_definitions_0,
            sampling_theta_index=0,
            start_event=0,
            end_event=last_train_index
        )
        n_actual_samples = x_0.shape[0]

        # Analyse theta values from theta0 run
        theta0_0 = thetas_0[0]
        theta1_0 = thetas_0[1]
        thetas_eval = thetas_0[2:]

        # Analyse augmented data from theta0 run
        r_xz_0 = augmented_data_0[0]
        t_xz0_0 = augmented_data_0[1]
        t_xz1_0 = augmented_data_0[2]

        r_xz_eval = []
        t_xz_eval = []
        for i, theta_eval in enumerate(thetas_eval):
            r_xz_eval.append(augmented_data_0[3 + i * 2])
            t_xz_eval.append(augmented_data_0[4 + i * 2])

        x_0 = np.vstack([x_0 for _ in range(1 + n_additional_thetas)])
        r_xz_0 = np.vstack([r_xz_0] + r_xz_eval)
        t_xz0_0 = np.vstack([t_xz0_0 for _ in range(1 + n_additional_thetas)])
        t_xz1_0 = np.vstack([t_xz1_0] + t_xz_eval)
        theta0_0 = np.vstack([theta0_0 for _ in range(1 + n_additional_thetas)])
        theta1_0 = np.vstack([theta1_0] + thetas_eval)

        # Parse thetas for theta1 sampling
        theta_types = []
        theta_values = []
        n_samples_per_theta = 1000000

        theta0_types, theta0_values, this_n_samples = parse_theta(theta0, n_samples // 2)
        theta_types.append(theta0_types)
        theta_values.append(theta0_values)
        n_samples_per_theta = min(this_n_samples, n_samples_per_theta)

        theta1_types, theta1_values, this_n_samples = parse_theta(theta1, n_samples // 2)
        theta_types.append(theta1_types)
        theta_values.append(theta1_values)
        n_samples_per_theta = min(this_n_samples, n_samples_per_theta)

        for additional_theta in additional_thetas:
            additional_theta_types, additional_theta_values, this_n_samples = parse_theta(additional_theta,
                                                                                          n_samples // 2)
            theta_types.append(additional_theta_types)
            theta_values.append(additional_theta_values)
            n_samples_per_theta = min(this_n_samples, n_samples_per_theta)

        # Start for theta1
        x_1, augmented_data_1, thetas_1 = self.extract_sample(
            theta_sets_types=theta_types,
            theta_sets_values=theta_values,
            n_samples_per_theta=n_samples_per_theta,
            augmented_data_definitions=augmented_data_definitions_1,
            sampling_theta_index=1,
            start_event=0,
            end_event=last_train_index
        )
        n_actual_samples += x_1.shape[0]

        # Analyse theta values from theta1 run
        theta0_1 = thetas_1[0]
        theta1_1 = thetas_1[1]
        thetas_eval = thetas_1[2:]

        # Analyse augmented data from theta1 run
        r_xz_1 = augmented_data_1[0]
        t_xz0_1 = augmented_data_1[1]
        t_xz1_1 = augmented_data_1[2]

        r_xz_eval = []
        t_xz_eval = []
        for i, theta_eval in enumerate(thetas_eval):
            r_xz_eval.append(augmented_data_1[3 + i * 2])
            t_xz_eval.append(augmented_data_1[4 + i * 2])

        x_1 = np.vstack([x_1 for _ in range(1 + n_additional_thetas)])
        r_xz_1 = np.vstack([r_xz_1] + r_xz_eval)
        t_xz0_1 = np.vstack([t_xz0_1] + t_xz_eval)
        t_xz1_1 = np.vstack([t_xz1_1 for _ in range(1 + n_additional_thetas)])
        theta0_1 = np.vstack([theta0_1] + thetas_eval)
        theta1_1 = np.vstack([theta1_1 for _ in range(1 + n_additional_thetas)])

        # Combine
        x = np.vstack([x_0, x_1])
        r_xz = np.vstack([r_xz_0, r_xz_1])
        t_xz0 = np.vstack([t_xz0_0, t_xz0_1])
        t_xz1 = np.vstack([t_xz1_0, t_xz1_1])
        theta0 = np.vstack([theta0_0, theta0_1])
        theta1 = np.vstack([theta1_0, theta1_1])
        y = np.zeros(x.shape[0])
        y[x_0.shape[0]:] = 1.

        if n_additional_thetas > 0:
            logging.info('Oversampling: created %s training samples from %s original unweighted events',
                         x.shape[0], n_actual_samples)

        # Shuffle
        x, r_xz, t_xz0, t_xz1, theta0, theta1, y = shuffle(x, r_xz, t_xz0, t_xz1, theta0, theta1, y)

        # y shape
        y = y.reshape((-1, 1))

        # Save data
        np.save(folder + '/theta0_' + filename + '.npy', theta0)
        np.save(folder + '/theta1_' + filename + '.npy', theta1)
        np.save(folder + '/x_' + filename + '.npy', x)
        np.save(folder + '/y_' + filename + '.npy', y)
        np.save(folder + '/r_xz_' + filename + '.npy', r_xz)
        np.save(folder + '/t_xz0_' + filename + '.npy', t_xz0)
        np.save(folder + '/t_xz1_' + filename + '.npy', t_xz1)

        return x, theta0, theta1, y, r_xz, t_xz0, t_xz1

    def extract_samples_test(self,
                             theta,
                             n_samples,
                             folder,
                             filename,
                             test_split=0.3):
        """
        Extracts evaluation samples x ~ p(x|theta).

        :param theta: tuple (type, value) that defines the parameter point or prior over parameter points used for the
                      sampling. Use the helper functions constant_benchmark_theta(), multiple_benchmark_thetas(),
                      constant_morphing_theta(), multiple_morphing_thetas(), or random_morphing_thetas().
        :param n_samples: Total number of samples to be drawn.
        :param folder: Folder for the resulting samples.
        :param filename: Label for the filenames. The actual filenames will add a prefix such as 'x_', and the extension
                         '.npy'.
        :param test_split: Fraction of events reserved for this evaluation sample (will not be used for any training
                           samples).
        """

        logging.info('Extracting evaluation sample. Sampling according to %s', theta)

        create_missing_folders([folder])

        # Thetas
        theta_types, theta_values, n_samples_per_theta = parse_theta(theta, n_samples)

        # Train / test split
        if test_split is None or test_split <= 0. or test_split >= 1.:
            first_test_index = 0
        else:
            first_test_index = int(round((1. - test_split) * self.n_samples, 0)) + 1

            if first_test_index < 0 or first_test_index > self.n_samples:
                raise ValueError("Irregular in train / test split: sample {} / {}", first_test_index, self.n_samples)

        # Extract information
        x, _, (theta,) = self.extract_sample(
            theta_sets_types=[theta_types],
            theta_sets_values=[theta_values],
            n_samples_per_theta=n_samples_per_theta,
            start_event=first_test_index,
            end_event=None
        )

        # Save data
        np.save(folder + '/theta_' + filename + '.npy', theta)
        np.save(folder + '/x_' + filename + '.npy', x)

        return x, theta

    def extract_cross_sections(self,
                               theta):

        """
        Calculates the total cross sections for all specified thetas.

        :param theta: tuple (type, value) that defines the parameter point or prior over parameter points used for the
                      sampling. Use the helper functions constant_benchmark_theta(), multiple_benchmark_thetas(),
                      constant_morphing_theta(), multiple_morphing_thetas(), or random_morphing_thetas().
        :return: thetas, xsecs, xsec_uncertainties. xsecs and xsec_uncertainties are in pb.
        """

        logging.info('Starting cross-section calculation')

        # Total xsecs for benchmarks
        xsecs_benchmarks = None
        squared_weight_sum_benchmarks = None

        for obs, weights in madminer_event_loader(self.madminer_filename):
            if xsecs_benchmarks is None:
                xsecs_benchmarks = np.sum(weights, axis=0)
                squared_weight_sum_benchmarks = np.sum(weights * weights, axis=0)
            else:
                xsecs_benchmarks += np.sum(weights, axis=0)
                squared_weight_sum_benchmarks += np.sum(weights * weights, axis=0)

        # Parse thetas for evaluation
        theta_types, theta_values, _ = parse_theta(theta, 1)

        # Loop over thetas
        all_thetas = []
        all_xsecs = []
        all_xsec_uncertainties = []

        for (theta_type, theta_value) in zip(theta_types, theta_values):

            if self.morpher is None and theta_type == 'morphing':
                raise RuntimeError('Theta defined through morphing, but no morphing setup has been loaded.')

            theta = get_theta_value(theta_type, theta_value, self.benchmarks)
            theta_matrix = get_theta_benchmark_matrix(
                theta_type,
                theta_value,
                self.benchmarks,
                self.morpher
            )

            # Total xsec for this theta
            xsec_theta = theta_matrix.dot(xsecs_benchmarks)
            rms_xsec_theta = ((theta_matrix * theta_matrix).dot(squared_weight_sum_benchmarks)) ** 0.5

            all_thetas.append(theta)
            all_xsecs.append(xsec_theta)
            all_xsec_uncertainties.append(rms_xsec_theta)

            logging.debug('theta %s: xsec = (%s +/- %s) pb', theta, xsec_theta, rms_xsec_theta)

        # Return
        all_thetas = np.array(all_thetas)
        all_xsecs = np.array(all_xsecs)
        all_xsec_uncertainties = np.array(all_xsec_uncertainties)

        return all_thetas, all_xsecs, all_xsec_uncertainties

    def extract_sample(self,
                       theta_sets_types,
                       theta_sets_values,
                       n_samples_per_theta,
                       sampling_theta_index=0,
                       augmented_data_definitions=None,
                       start_event=0,
                       end_event=None):
        """
        Low-level function for the extraction of information from the event samples.

        :param theta_sets_types: list of lists of str, each entry can be 'benchmark' or 'morphing'
        :param theta_sets_values: list of lists, each entry is int and labels the benchmark index (if the corresponding
                                      theta_sampling_types entry is 'benchmark') or a numpy array with the theta values
                                      (of the corresponding theta_sampling_types entry is 'morphing')
        :param n_samples_per_theta: Number of samples to be drawn per entry in theta_sampling_types.
        :param augmented_data_definitions: list of tuples. Each tuple can either be ('ratio', num_theta, den_theta) or
                                           ('score', theta), where num_theta, den_theta, and theta are indexes marking
                                           which of the theta sets defined through thetas_types and thetas_values is
                                           used.
        :param sampling_theta_index: int, marking the index of the theta set defined through thetas_types and
                                     thetas_values that should be used for sampling
        :param start_event: Index of first event to consider.
        :param end_event: Index of last event to consider.
        :return: tuple (x, augmented_data_list, theta_sampling, theta_auxiliary). x, theta_sampling, theta_auxiliary,
                 and all elements of the list augmented_data_list are ndarrays with the number of samples as first
                 dimension.
        """

        logging.debug('Starting sample extraction')

        if augmented_data_definitions is None:
            augmented_data_definitions = []

        logging.debug('Augmented data requested:')
        for augmented_data_definition in augmented_data_definitions:
            logging.debug('  %s', augmented_data_definition)

        # Calculate total xsecs for benchmarks
        xsecs_benchmarks = None
        squared_weight_sum_benchmarks = None
        n_observables = 0

        for obs, weights in madminer_event_loader(self.madminer_filename, start=start_event, end=end_event):
            if xsecs_benchmarks is None:
                xsecs_benchmarks = np.sum(weights, axis=0)
                squared_weight_sum_benchmarks = np.sum(weights * weights, axis=0)
            else:
                xsecs_benchmarks += np.sum(weights, axis=0)
                squared_weight_sum_benchmarks += np.sum(weights * weights, axis=0)

            n_observables = obs.shape[1]

        logging.debug('Benchmark cross sections [pb]: %s', xsecs_benchmarks)

        # Balance thetas
        theta_sets_types, theta_sets_values = balance_thetas(theta_sets_types, theta_sets_values)

        # Consistency checks
        n_benchmarks = xsecs_benchmarks.shape[0]
        if n_benchmarks != len(self.benchmarks) and self.morphing_matrix is None:
            raise ValueError('Inconsistent numbers of benchmarks: {} in observations,'
                             '{} in benchmark list'.format(n_benchmarks, len(self.benchmarks)))
        elif n_benchmarks != len(self.benchmarks) or n_benchmarks != self.morphing_matrix.shape[0]:
            raise ValueError('Inconsistent numbers of benchmarks: {} in observations, {} in benchmark list, '
                             '{} in morphing matrix'.format(n_benchmarks, len(self.benchmarks),
                                                            self.morphing_matrix.shape[0]))

        if n_observables != len(self.observables):
            raise ValueError('Inconsistent numbers of observables: {} in observations,'
                             '{} in observable list'.format(n_observables, len(self.observables)))

        n_thetas = len(theta_sets_types)
        assert n_thetas == len(theta_sets_values)

        n_sets = len(theta_sets_types[sampling_theta_index])  # Within each set, all thetas (sampling, numerator, ...)
                                                              # have a constant value
        for theta_types, theta_values in zip(theta_sets_types, theta_sets_values):
            assert n_sets == len(theta_types) == len(theta_values)

        # Number of samples to be drawn
        if not isinstance(n_samples_per_theta, collections.Iterable):
            n_samples_per_theta = [n_samples_per_theta] * n_sets
        elif len(n_samples_per_theta) == 1:
            n_samples_per_theta = [n_samples_per_theta[0]] * n_sets

        # Prepare output
        all_x = []
        all_augmented_data = [[] for _ in augmented_data_definitions]
        all_thetas = [[] for _ in range(n_thetas)]

        # Main loop over thetas
        for i_set in range(n_sets):

            n_samples = n_samples_per_theta[i_set]

            theta_types = [t[i_set] for t in theta_sets_types]
            theta_values = [t[i_set] for t in theta_sets_values]

            if self.morpher is None and 'morphing' in theta_types:
                raise RuntimeError('Theta defined through morphing, but no morphing setup has been loaded.')

            # Parse thetas and calculate the w_c(theta) for them
            thetas = []
            theta_matrices = []
            theta_gradient_matrices = []

            logging.debug('Drawing %s events for the following thetas:', n_samples)

            for i_theta, (theta_type, theta_value) in enumerate(zip(theta_types, theta_values)):
                theta = get_theta_value(theta_type, theta_value, self.benchmarks)
                theta = np.broadcast_to(theta, (n_samples, theta.size))
                thetas.append(theta)

                theta_matrices.append(
                    get_theta_benchmark_matrix(theta_type, theta_value, self.benchmarks, self.morpher)
                )
                theta_gradient_matrices.append(
                    get_dtheta_benchmark_matrix(theta_type, theta_value, self.benchmarks, self.morpher)
                )

                logging.debug('  theta %s = %s%s',
                              i_theta, theta[0,:],
                              ' (sampling)' if i_theta == sampling_theta_index else '')

            sampling_theta_matrix = theta_matrices[sampling_theta_index]

            # Total xsec for sampling theta
            xsec_sampling_theta = sampling_theta_matrix.dot(xsecs_benchmarks)
            rms_xsec_sampling_theta = ((sampling_theta_matrix * sampling_theta_matrix).dot(
                squared_weight_sum_benchmarks)) ** 0.5

            if rms_xsec_sampling_theta > 0.2 * xsec_sampling_theta:
                logging.warning('Warning: large statistical uncertainty on the total cross section for theta = %s: '
                                '(%s +/- %s) pb',
                                thetas[sampling_theta_index][0],
                                xsec_sampling_theta,
                                rms_xsec_sampling_theta)

            # Prepare output
            samples_done = np.zeros(n_samples, dtype=np.bool)
            samples_x = np.zeros((n_samples, n_observables))
            samples_augmented_data = []
            for definition in augmented_data_definitions:
                if definition[0] == 'ratio':
                    samples_augmented_data.append(np.zeros((n_samples, 1)))
                elif definition[0] == 'score':
                    samples_augmented_data.append(np.zeros((n_samples, self.n_parameters)))

            # Main sampling loop
            while not np.all(samples_done):

                # Draw random numbers in [0, 1]
                u = np.random.rand(n_samples)  # Shape: (n_samples,)

                # Loop over weighted events
                cumulative_p = np.array([0.])

                for x_batch, weights_benchmarks_batch in madminer_event_loader(self.madminer_filename,
                                                                               start=start_event,
                                                                               end=end_event):
                    # Evaluate p(x | sampling theta)
                    weights_theta = sampling_theta_matrix.dot(weights_benchmarks_batch.T)  # Shape (n_batch_size,)
                    p_theta = weights_theta / xsec_sampling_theta  # Shape: (n_batch_size,)

                    # Handle negative weights (should be rare)
                    n_negative_weights = np.sum(p_theta < 0.)
                    if n_negative_weights > 0:
                        logging.warning('%s negative weights (%s)',
                                        n_negative_weights, n_negative_weights / p_theta.size)
                    p_theta[p_theta < 0.] = 0.

                    # Calculate cumulative p (summing up all events until here)
                    cumulative_p = cumulative_p.flatten()[-1] + np.cumsum(p_theta)  # Shape: (n_batch_size,)

                    # When cumulative_p hits u, we store the events
                    indices = np.searchsorted(cumulative_p, u, side='left').flatten()
                    # Shape: (n_samples,), values: [0, ..., n_batch_size]

                    found_now = (np.invert(samples_done) & (indices < len(cumulative_p)))  # Shape: (n_samples,)
                    samples_x[found_now] = x_batch[indices[found_now]]
                    samples_done[found_now] = True

                    # Extract augmented data
                    relevant_augmented_data = extract_augmented_data(
                        augmented_data_definitions,
                        weights_benchmarks_batch[indices[found_now], :],
                        xsecs_benchmarks,
                        theta_matrices,
                        theta_gradient_matrices,
                    )
                    for i, this_relevant_augmented_data in enumerate(relevant_augmented_data):
                        samples_augmented_data[i][found_now] = this_relevant_augmented_data

                    if np.all(samples_done):
                        break

                # Cross-check cumulative probabilities at end
                logging.debug('  Cumulative probability (should be close to 1): %s', cumulative_p[-1])

                # Check that we got 'em all, otherwise repeat
                if not np.all(samples_done):
                    logging.debug(
                        '  After full pass through event files, {} / {} samples not found, u = {}'.format(
                            np.sum(np.invert(samples_done)),
                            samples_done.size,
                            u[np.invert(samples_done)]
                        ))

            all_x.append(samples_x)
            for i, theta in enumerate(thetas):
                all_thetas[i].append(theta)
            for i, this_samples_augmented_data in enumerate(samples_augmented_data):
                all_augmented_data[i].append(this_samples_augmented_data)

        # Combine and return results
        all_x = np.vstack(all_x)
        for i in range(n_thetas):
            all_thetas[i] = np.vstack(all_thetas[i])
        for i in range(len(all_augmented_data)):
            all_augmented_data[i] = np.vstack(all_augmented_data[i])

        return all_x, all_augmented_data, all_thetas

    def extract_raw_data(self, theta=None):

        """

        :param theta: if not None, uses morphing to calculate the weights for this value of theta. If None, returns
                      the weights in fb for all benchmark points, as in the file.
        :return: x, weights
        """

        x, weights_benchmarks = next(madminer_event_loader(self.madminer_filename, batch_size=None))

        if theta is not None:
            theta_matrix = get_theta_benchmark_matrix(
                'morphing',
                theta,
                self.benchmarks,
                self.morpher
            )

            weights_theta = theta_matrix.dot(weights_benchmarks.T)

            return x, weights_theta

        return x, weights_benchmarks
