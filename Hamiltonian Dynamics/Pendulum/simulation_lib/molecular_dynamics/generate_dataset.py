# Copyright 2020 DeepMind Technologies Limited.
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Creates a small molecular dynamics (MD) dataset.

This binary creates a small MD dataset given a text-based trajectory file
generated with the simulation package LAMMPS (https://lammps.sandia.gov/).
The trajectory file can be generated by running LAMMPS with the input script
provided.

We note that this binary is intended as a demonstration only and is therefore
not optimised for memory efficiency or performance.
"""

from typing import Mapping, Tuple

from simulation_lib import datasets
from simulation_lib.hamiltonian_systems import utils

import numpy as np

Array = np.ndarray


def read_trajectory(filename: str) -> Tuple[Array, float]:
  """Reads the trajectory data from file and returns it as an array.

  Each timestep contains a header and the atom data. The header is 9 lines long
  and contains the timestep, the number of atoms, the box dimensions and the
  list of atom properties. The header is assumed to be structured as in the
  example below:

    ITEM: TIMESTEP
    <<timestep>>
    ITEM: NUMBER OF ATOMS
    <<num_atoms>>
    ITEM: BOX BOUNDS pp pp pp
    <<xlo>> <<xhi>>
    <<ylo>> <<yhi>>
    <<zlo>> <<zhi>>
    ITEM: ATOMS id type x y vx vy fx fy
    .... <<num_atoms>> lines with properties of <<num_atoms>> atoms....

  Args:
    filename: name of the input file.

  Returns:
    A pair where the first element corresponds to an array of shape
    [num_timesteps, num_atoms, 6] containing the atom data and the second
    element corresponds to the edge length of the simulation box.
  """
  with open(filename, 'r') as f:
    dat = f.read()
  lines = dat.split('\n')

  # Extract the number of particles and the edge length of the simulation box.
  num_particles = int(lines[3])
  box = np.fromstring(lines[5], dtype=np.float32, sep=' ')
  box_length = box[1] - box[0]

  # Iterate over all timesteps and extract the relevant data columns.
  header_size = 9
  record_size = header_size + num_particles
  num_records = len(lines) // record_size
  records = []
  for i in range(num_records):
    record = lines[header_size + i * record_size:(i + 1) * record_size]
    record = np.array([l.split(' ')[:-1] for l in record], dtype=np.float32)
    records.append(record)
  records = np.array(records)[..., 2:]
  return records, box_length


def flatten_record(x: Array) -> Array:
  """Reshapes input from [num_particles, 2*dim] to [2*num_particles*dim]."""
  if x.shape[-1] % 2 != 0:
    raise ValueError(f'Expected last dimension to be even, got {x.shape[-1]}.')
  dim = x.shape[-1] // 2
  q = x[..., 0:dim]
  p = x[..., dim:]
  q_flat = q.reshape(list(q.shape[:-2]) + [-1])
  p_flat = p.reshape(list(p.shape[:-2]) + [-1])
  x_flat = np.concatenate((q_flat, p_flat), axis=-1)
  return x_flat


def render_images(
    x: Array,
    box_length: float,
    resolution: int = 32,
    particle_radius: float = 0.3
) -> Array:
  """Renders a sequence with shape [num_steps, num_particles*dim] as images."""
  dim = 2
  sequence_length, num_coordinates = x.shape
  if num_coordinates % (2 * dim) != 0:
    raise ValueError('Expected the number of coordinates to be divisible by 4, '
                     f'got {num_coordinates}.')
  # The 4 coordinates are positions and velocities in 2d.
  num_particles = num_coordinates // (2 * dim)
  # `x` is formatted as [x_1, y_1,... x_N, y_N, dx_1, dy_1,..., dx_N, dy_N],
  # where `N=num_particles`. For the image generation, we only require x and y
  # coordinates.
  particles = x[..., :num_particles * dim]
  particles = particles.reshape((sequence_length, num_particles, dim))
  colors = np.arange(num_particles, dtype=np.int32)
  box_region = utils.BoxRegion(-box_length / 2., box_length / 2.)
  images = utils.render_particles_trajectory(
      particles=particles,
      particles_radius=particle_radius,
      color_indices=colors,
      canvas_limits=box_region,
      resolution=resolution,
      num_colors=num_particles)
  return images


def convert_sequence(sequence: Array, box_length: float) -> Mapping[str, Array]:
  """Converts a sequence of timesteps to a data point."""
  num_steps, num_particles, num_fields = sequence.shape
  # A LAMMPS record should contain positions, velocities and forces.
  if num_fields != 6:
    raise ValueError('Expected input sequence to be of shape '
                     f'[num_steps, num_particles, 6], got {sequence.shape}.')
  x = np.empty((num_steps, num_particles * 4))
  dx_dt = np.empty((num_steps, num_particles * 4))
  for step in range(num_steps):
    # Assign positions and momenta to `x` and momenta and forces to `dx_dt`.
    x[step] = flatten_record(sequence[step, :, (0, 1, 2, 3)])
    dx_dt[step] = flatten_record(sequence[step, :, (2, 3, 4, 5)])

  image = render_images(x, box_length)
  image = np.array(image * 255.0, dtype=np.uint8)
  return dict(x=x, dx_dt=dx_dt, image=image)


def write_to_file(
    data: Array,
    box_length: float,
    output_path: str,
    split: str,
    overwrite: bool,
) -> None:
  """Writes the data to file."""

  def generator():
    for sequence in data:
      yield convert_sequence(sequence, box_length)

  datasets.transform_dataset(generator(), output_path, split, overwrite)


def generate_lammps_dataset(
    lammps_file: str,
    folder: str,
    num_steps: int,
    num_train: int,
    num_test: int,
    dt: int,
    shuffle: bool,
    seed: int,
    overwrite: bool,
) -> None:
  """Creates the train and test datasets."""
  if num_steps < 1:
    raise ValueError(f'Expected `num_steps` to be >= 1, got {num_steps}.')
  if dt < 1:
    raise ValueError(f'Expected `dt` to be >= 1, got {dt}.')

  records, box_length = read_trajectory(lammps_file)
  # Consider only every dt-th timestep in the input file.
  records = records[::dt]
  num_records, num_particles, num_fields = records.shape
  if num_records < (num_test + num_train) * num_steps:
    raise ValueError(
        f'Trajectory contains only {num_records} records which is insufficient'
        f'for the requested train/test split of {num_train}/{num_test} with '
        f'sequence length {num_steps}.')

  # Reshape and shuffle the data.
  num_points = num_records // num_steps
  records = records[:num_points * num_steps]
  records = records.reshape((num_points, num_steps, num_particles, num_fields))
  if shuffle:
    np.random.RandomState(seed).shuffle(records)

  # Create train/test splits and write them to file.
  train_records = records[:num_train]
  test_records = records[num_train:num_train + num_test]
  print('Writing the train dataset to file.')
  write_to_file(train_records, box_length, folder, 'train', overwrite)
  print('Writing the test dataset to file.')
  write_to_file(test_records, box_length, folder, 'test', overwrite)
