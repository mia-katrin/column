import copy
import multiprocessing as mp
import random
import time

import numpy as np
import tensorflow as tf
from numba import jit
from src.animate import animate
from src.perception_matrix import get_perception_matrix
from src.utils import (
    add_channels_single_preexisting,
    get_model_weights,
    get_weights_info,
)
from tensorflow.keras.layers import Conv2D, Dense, Input
from tensorflow.python.framework.convert_to_constants import (
    convert_variables_to_constants_v2,
)


class MovingNCA(tf.keras.Model):
    def __init__(
        self,
        num_classes,
        num_hidden,
        hidden_neurons=10,
        iterations=50,
        position="None",
        size_neo=None,
        size_image=None,
        moving=True,
        mnist_digits=(0, 3, 4),
    ):
        super().__init__()

        if size_image is None:
            size_image = (28, 28)
        self.size_neo = get_dimensions(size_image, size_neo[0], size_neo[1])
        self.size_image = size_image
        self._size_active = (size_image[0] - 2, size_image[1] - 2)

        self.img_channels = 1
        self.act_channels = 2
        self.num_classes = num_classes
        self.num_hidden = num_hidden
        self.input_dim = self.img_channels + self.num_hidden + self.num_classes
        self.output_dim = self.num_hidden + self.num_classes + self.act_channels
        self.iterations = iterations
        self.moving = moving
        self.position = position
        self.position_addon = 0 if self.position == None else 2

        self.mnist_digits = mnist_digits

        # Adjustable size
        self.dmodel = tf.keras.Sequential(
            [
                Dense(
                    hidden_neurons,
                    input_dim=self.input_dim * 3 * 3 + self.position_addon,
                    activation="linear",
                ),
                Dense(self.output_dim, activation="linear"),  # or linear
            ]
        )

        self.reset()

        # dummy calls to build the model
        self.dmodel(tf.zeros([1, self.input_dim * 3 * 3 + self.position_addon]))

    def reset(self):
        """
        Resets the state by resetting the dmodel layers, the state and the perception matrix
        """
        self.dmodel.reset_states()  # Reset the state if any dmodel.layers is stateful. If not, does nothing.
        self.perceptions = get_perception_matrix(
            self.size_image[0] - 2, self.size_image[1] - 2, self.size_neo[0], self.size_neo[1]
        )
        # The internal state of the artificial neocortex needs to be reset as well
        self.state = np.zeros((self.size_image[0], self.size_image[1], self.input_dim - self.img_channels))

    def reset_batched(self, batch_size):
        self.dmodel.reset_states()
        self.perceptions_batched = []

        for _ in range(batch_size):
            self.perceptions_batched.append(
                get_perception_matrix(
                    self.size_image[0] - 2, self.size_image[1] - 2, self.size_neo[0], self.size_neo[1]
                )
            )
        self.perceptions_batched = np.array(self.perceptions_batched)

        self.state_batched = np.zeros(
            (batch_size, self.size_image[0], self.size_image[1], self.input_dim - self.img_channels)
        )

    # @tf.function
    def call(self, img, visualize=False):
        return self.classify(img, visualize)

    def predict_step(self, data):
        # return super().predict_step(data)

        return NotImplementedError()

    def classify_batch(self, images_raw, visualize=False):
        B = len(images_raw)
        N_neo, M_neo = self.size_neo
        N_active, M_active = self._size_active

        guesses = None
        for _ in range(self.iterations):
            input = np.empty((B * N_neo * M_neo, 3 * 3 * self.input_dim + self.position_addon))
            # start_time = time.time()
            collect_input_batched(
                input, images_raw, self.state_batched, self.perceptions_batched, self.position, N_neo, M_neo
            )
            # print("Collecting took", time.time() - start_time)
            # start_time = time.time()
            guesses = self.dmodel(input)
            # print("Call took", time.time() - start_time)
            guesses = guesses.numpy()
            # start_time = time.time()
            outputs = np.reshape(guesses[:, :], (B, N_neo, M_neo, self.output_dim))
            # print("Reshape took", time.time() - start_time)

            # start_time = time.time()
            self.state_batched[:, 1 : 1 + N_neo, 1 : 1 + M_neo, :] = (
                self.state_batched[:, 1 : 1 + N_neo, 1 : 1 + M_neo, :]
                + outputs[:, :, :, : self.input_dim - self.img_channels]
            )
            # print("State took", time.time() - start_time)

            start_time = time.time()
            if self.moving:
                alter_perception_slicing_batched(
                    self.perceptions_batched, outputs[:, :, :, -self.act_channels :], N_neo, M_neo, N_active, M_active
                )
            # print("Slicing took", time.time() - start_time)

        return self.state_batched[:, 1 : 1 + N_neo, 1 : 1 + M_neo, -self.num_classes :], guesses

    def classify(self, img_raw, visualize=False, silenced=0):
        """
        Classify the input image using the trained model.

        Parameters:
            img_raw (np.ndarray): The raw input image.
            visualize (bool, optional): Whether to visualize the classification process. Defaults to False.

        Returns:
            np.ndarray: The state of the model after classification.
            np.ndarray: The guesses made by the model.
        """

        if visualize:
            images = []
            perceptions_through_time = []
            outputs_through_time = []
            actions = []

        N_neo, M_neo = self.size_neo
        N_active, M_active = self._size_active

        if silenced > 0:
            x = np.random.randint(N_neo)
            y = np.random.randint(M_neo)

            radius = silenced
            random_x, random_y = [], []
            for i in range(N_neo):
                for j in range(M_neo):
                    if np.sqrt((i - x) ** 2 + (j - y) ** 2) < radius:
                        random_x.append(i)
                        random_y.append(j)

            random_x = np.array(random_x) + 1
            random_y = np.array(random_y) + 1

            """x, y = np.meshgrid(list(range(N_neo)), list(range(M_neo)))
            xy = [x.ravel(), y.ravel()]
            indices = np.array(xy).T

            random_indices = np.random.choice(range(len(indices)), size=silenced, replace=False)

            random_x, random_y = indices[random_indices].T + 1"""

        guesses = None
        for _ in range(self.iterations):
            start_time = time.time()
            input = np.empty((N_neo * M_neo, 3 * 3 * self.input_dim + self.position_addon))
            # print("Preprocessing time:", time.time() - start_time)
            start_time = time.time()
            collect_input(input, img_raw, self.state, self.perceptions, self.position, N_neo, M_neo)
            # print("Input collection time:", time.time() - start_time)

            start_time = time.time()
            # guesses = tf.stop_gradient(self.dmodel(input)) # This doesn't make a difference
            guesses = self.dmodel(input)
            # print("Model call time:", time.time() - start_time)
            start_time = time.time()
            guesses = guesses.numpy()
            # print("Postprocessing time:", time.time() - start_time)

            start_time = time.time()
            outputs = np.reshape(guesses[:, :], (N_neo, M_neo, self.output_dim))
            # print("Reshaping time:", time.time() - start_time)

            self.state[1 : 1 + N_neo, 1 : 1 + M_neo, :] = (
                self.state[1 : 1 + N_neo, 1 : 1 + M_neo, :] + outputs[:, :, : self.input_dim - self.img_channels]
            )

            if silenced > 0:
                self.state[random_x, random_y, :] = 0

            if self.moving:
                start_time = time.time()
                alter_perception_slicing(
                    self.perceptions, outputs[:, :, -self.act_channels :], N_neo, M_neo, N_active, M_active
                )
                # print("Slicing time:", time.time() - start_time)

            if visualize:
                img = add_channels_single_preexisting(img_raw, self.state)
                images.append(copy.deepcopy(img))
                perceptions_through_time.append(copy.deepcopy(self.perceptions))
                outputs_through_time.append(copy.deepcopy(outputs))
                actions.append(copy.deepcopy(outputs[:, :, -self.act_channels :]))

        if visualize:
            self.visualize(
                images, perceptions_through_time, actions, self.num_hidden, self.num_classes, self.mnist_digits
            )

        return self.state[1 : 1 + N_neo, 1 : 1 + M_neo, -self.num_classes :], guesses

    def visualize(self, images, perceptions_through_time, actions, HIDDEN_CHANNELS, CLASS_CHANNELS, MNIST_DIGITS):
        # It's slower, however the animate function spawns many objects and leads to memory leaks. By using the
        # function in a new process, all objects should be cleaned up at close and the animate function
        # can be used as many times as wanted
        p = mp.Process(
            target=animate,
            args=(images, perceptions_through_time, actions, HIDDEN_CHANNELS, CLASS_CHANNELS, MNIST_DIGITS),
        )
        p.start()
        p.join()
        p.close()
        # animate(images, perceptions_through_time) # Leads to memory leaks

    @staticmethod
    def get_instance_with(
        flat_weights,
        num_classes,
        num_hidden,
        hidden_neurons,
        iterations,
        position,
        size_neo=None,
        size_image=None,
        moving=True,
        mnist_digits=(0, 3, 4),
    ):
        network = MovingNCA(
            num_classes=num_classes,
            num_hidden=num_hidden,
            hidden_neurons=hidden_neurons,
            iterations=iterations,
            position=position,
            size_neo=size_neo,
            size_image=size_image,
            moving=moving,
            mnist_digits=mnist_digits,
        )
        network.set_weights(flat_weights)
        return network

    def set_weights(self, flat_weights):
        weight_shape_list, weight_amount_list, _ = get_weights_info(self.weights)
        shaped_weight = get_model_weights(flat_weights, weight_amount_list, weight_shape_list)
        self.dmodel.set_weights(shaped_weight)

        return None  # Why does it explicitly return None?


def custom_round_slicing(x: list):
    """
    Rounds the values in the input list by applying slicing.
    Negative values are rounded down to -1, positive values are rounded
    up to 1, and zero values are rounded to 0.

    Parameters:
        x (list): The input list of values.

    Returns:
        list: The list of rounded values.
    """
    x_new = np.zeros(x.shape, dtype=np.int64)
    negative = x < -0.0007
    positive = x > 0.0007
    zero = np.logical_not(np.logical_or(positive, negative))
    # zero = ~ (positive + negative) Markus suggests this

    x_new[negative] = -1
    x_new[positive] = 1
    x_new[zero] = 0

    return x_new


@jit
def clipping(array, N, M):
    # This function clips the values in the array to the range [0, N]
    # It alters the array in place
    for x in range(len(array)):
        for y in range(len(array[0])):
            array[x, y, 0] = min(max(array[x, y, 0], 0), N)
            array[x, y, 1] = min(max(array[x, y, 1], 0), M)


def add_action_slicing(perception: list, action: list, N: int, M: int) -> np.ndarray:
    perception += custom_round_slicing(action)
    assert N == M, "The code currently does not support N != M"
    clipping(perception, N - 1, M - 1)  # Changes array in place


def alter_perception_slicing(perceptions, actions, N_neo, M_neo, N_active, M_active):
    # TODO: Remove this fucntion, you only need the one below
    add_action_slicing(perceptions, actions, N_active, M_active)


@jit
def clipping_batched(array, N, M):
    # This function clips the values in the array to the range [0, N]
    # It alters the array in place
    B, N_neo, M_neo, _ = array.shape
    for b in range(B):
        for x in range(N_neo):
            for y in range(M_neo):
                array[b, x, y, 0] = min(max(array[b, x, y, 0], 0), N)
                array[b, x, y, 1] = min(max(array[b, x, y, 1], 0), M)


def add_action_slicing_batched(perceptions_batched: list, actions_batched: list, N: int, M: int) -> np.ndarray:
    perceptions_batched += custom_round_slicing(actions_batched)
    assert N == M, "The code currently does not support N != M"
    clipping_batched(perceptions_batched, N - 1, M - 1)  # Changes array in place


def alter_perception_slicing_batched(perceptions_batched, actions_batched, N_neo, M_neo, N_active, M_active):
    add_action_slicing_batched(perceptions_batched, actions_batched, N_active, M_active)


@jit
def collect_input(input, img, state, perceptions, position, N_neo, M_neo):
    N, M, _ = state.shape
    for x in range(N_neo):
        for y in range(M_neo):
            x_p, y_p = perceptions[x, y]
            perc = img[x_p : x_p + 3, y_p : y_p + 3, :1]
            comms = state[x : x + 3, y : y + 3, :]
            dummy = np.concatenate((perc, comms), axis=2)
            dummy_flat = dummy.flatten()
            input[x * M_neo + y, : len(dummy_flat)] = dummy_flat

            # When position is None, the input is just the perception and comms
            if position == "current":
                input[x * M_neo + y, -2] = (x_p - N // 2) / (N // 2)
                input[x * M_neo + y, -1] = (y_p - M // 2) / (M // 2)
            elif position == "initial":
                input[x * M_neo + y, -2] = (float(x) * N / float(N_neo) - N // 2) / (N // 2)
                input[x * M_neo + y, -1] = (float(y) * M / float(M_neo) - M // 2) / (M // 2)


@jit
def collect_input_batched(input, images, state_batched, perceptions_batched, position, N_neo, M_neo):
    B, N, M, _ = images.shape

    for x in range(N_neo):
        for y in range(M_neo):
            for b in range(B):
                x_p, y_p = perceptions_batched[b, x, y].T
                perc = images[b, x_p : x_p + 3, y_p : y_p + 3, :]
                comms = state_batched[b, x : x + 3, y : y + 3, :]

                dummy = np.concatenate((perc, comms), axis=-1)
                dummy_flat = dummy.flatten()
                input[b * N_neo * M_neo + x * M_neo + y, : len(dummy_flat)] = dummy_flat

                # When position is None, the input is just the perception and comms
                if position == "current":
                    input[b * N_neo * M_neo + x * M_neo + y, -2] = (x_p - N // 2) / (N // 2)
                    input[b * N_neo * M_neo + x * M_neo + y, -1] = (y_p - M // 2) / (M // 2)
                elif position == "initial":
                    input[b * N_neo * M_neo + x * M_neo + y, -2] = (float(x) * N / float(N_neo) - N // 2) / (N // 2)
                    input[b * N_neo * M_neo + x * M_neo + y, -1] = (float(y) * M / float(M_neo) - M // 2) / (M // 2)

    """for i in range(B):
        input_inner = np.empty((N_neo * M_neo, input.shape[1]))
        collect_input(
            input_inner,
            images[i],
            state_batched[i],
            perceptions_batched[i],
            position,
            N_neo,
            M_neo,
        )
        input[i * N_neo * M_neo : (i + 1) * N_neo * M_neo] = input_inner"""


def get_dimensions(data_shape, N_neo, M_neo):
    N, M = data_shape
    N_neo = N - 2 if N_neo is None else N_neo
    M_neo = M - 2 if M_neo is None else M_neo
    return N_neo, M_neo


@jit
def expand(arr):
    B, N, M, _ = arr.shape
    expanded_arr = np.zeros((B, N * 3, M * 3, 2), dtype=np.int32)
    x_p, y_p = arr[:, :, :, 0], arr[:, :, :, 1]

    for i in range(3):
        for j in range(3):
            expanded_arr[:, i::3, j::3, 0] = x_p + i
            expanded_arr[:, i::3, j::3, 1] = y_p + j

    return expanded_arr  # Fucking "in16" is not supported...


@jit
def gather_mine(arr, movement_expanded):
    B, N_neo, M_neo, _ = movement_expanded.shape
    new_arr = np.empty((B, N_neo, M_neo, arr.shape[-1]), dtype=arr.dtype)

    for b in range(B):
        for x in range(N_neo):
            for y in range(M_neo):
                new_arr[b, x, y, :] = arr[b, movement_expanded[b, x, y, 0], movement_expanded[b, x, y, 1], :]

    return new_arr
