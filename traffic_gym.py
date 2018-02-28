import bisect

import pygame, pdb, torch
import math
import random
import numpy as np
import sys
from custom_graphics import draw_dashed_line, draw_text, draw_rect
from gym import core

seed = 123
random.seed(seed)
np.random.seed(seed)

# Conversion LANE_W from real world to pixels
# A US highway lane width is 3.7 metres, here 50 pixels
LANE_W = 20  # pixels / 3.7 m, lane width
SCALE = LANE_W / 3.7

colours = {
    'w': (255, 255, 255),
    'k': (000, 000, 000),
    'r': (255, 000, 000),
    'g': (000, 255, 000),
    'm': (255, 000, 255),
    'b': (000, 000, 255),
    'c': (000, 255, 255),
    'y': (255, 255, 000),
}

# Car coordinate system, origin under the centre of the read axis
#
#      ^ y                       (x, y, x., y.)
#      |
#   +--=-------=--+
#   |  | z        |
# -----o-------------->
#   |  |          |    x
#   +--=-------=--+
#      |
#
# Will approximate this as having the rear axis on the back of the car!
#
# Car sizes:
# type    | width [m] | length [m]
# ---------------------------------
# Sedan   |    1.8    |    4.8
# SUV     |    2.0    |    5.3
# Compact |    1.7    |    4.5

class Car:
    def __init__(self, lanes, free_lanes, dt, car_id):
        """
        Initialise a sedan on a random lane
        :param lanes: tuple of lanes, with ``min`` and ``max`` y coordinates
        :param dt: temporal updating interval
        """
        self._length = round(4.8 * SCALE)
        self._width = round(1.8 * SCALE)
        self._direction = np.array((1, 0), np.float)
        self._id = car_id
        lane = random.choice(tuple(free_lanes))
        self._position = np.array((
            -self._length,
            lanes[lane]['mid']
        ), np.float)
        self._target_speed = (random.randrange(115, 130) - 10 * lane) * 1000 / 3600 * SCALE  # m / s
        self._speed = self._target_speed
        self._dt = dt
        self._colour = colours['c']
        self._braked = False
        self._passing = False
        self._target_lane = self._position[1]
        self.crashed = False
        self._error = 0
        self._states = []
        self._actions = []

    def get_state(self):
        state = torch.zeros(4)
        state[0] = self._position[0]  # x
        state[1] = self._position[1]  # y
        state[2] = self._direction[0] * self._speed  # dx
        state[3] = self._direction[1] * self._speed  # dy
        return state

    def get_obs(self, left_vehicles, mid_vehicles, right_vehicles):
        n_cars = 1 + 6  # this car + 6 neighbors
        obs = torch.zeros(n_cars, 2, 2)
        mask = torch.zeros(n_cars)
        obs = obs.view(n_cars, 4)

        obs[0].copy_(self.get_state())

        if left_vehicles[0] is not None:
            obs[1].copy_(left_vehicles[0].get_state())
            mask[1] = 1
        else:
            # for bag-of-cars this will be ignored by the mask, 
            # but fill in with a similar value to not mess up batch norm
            obs[1].copy_(self.get_state())

        if left_vehicles[1] is not None:
            obs[2].copy_(left_vehicles[1].get_state())
            mask[2] = 1
        else:
            obs[2].copy_(self.get_state())

        if mid_vehicles[0] is not None:
            obs[3].copy_(mid_vehicles[0].get_state())
            mask[3] = 1
        else:
            obs[3].copy_(self.get_state())

        if mid_vehicles[1] is not None:
            obs[4].copy_(mid_vehicles[1].get_state())
            mask[4] = 1
        else:
            obs[4].copy_(self.get_state())

        if right_vehicles[0] is not None:
            obs[5].copy_(right_vehicles[0].get_state())
            mask[5] = 1
        else:
            obs[5].copy_(self.get_state())

        if right_vehicles[1] is not None:
            obs[6].copy_(right_vehicles[1].get_state())
            mask[6] = 1
        else:
            obs[6].copy_(self.get_state())

        return obs, mask

    def draw(self, screen, c=None):
        """
        Draw current car on screen with a specific colour
        :param screen: PyGame ``Surface`` where to draw
        :param c: default colour
        """
        x, y = self._position
        rectangle = (int(x), int(y), self._length, self._width)
        if c is not None:
            self._colour = c
        draw_rect(screen, self._colour, rectangle, 3, self._direction)
        if self._braked: self._colour = colours['g']

    def step(self, action):  # takes also the parameter action = state temporal derivative
        """
        Update current position, given current velocity and acceleration
        """
        # Vehicle state definition
        vehicle_state = np.array((*self._position, *self._direction, self._speed))
        # State integration
        d_position_dt = self._speed * self._direction
        vehicle_state[:2] += d_position_dt * self._dt
        vehicle_state[2:] += action * self._dt

        # Split individual components (and normalise direction)
        self._position = vehicle_state[0:2]
        self._direction = vehicle_state[2:4] / np.sqrt(np.linalg.norm(vehicle_state[2:4]))
        self._speed = vehicle_state[4]

        # Deal with latent variable and visual indicator
        if self._passing and self._error == 0:
            self._passing = False
            self._colour = colours['c']

    def get_lane_set(self, lanes):
        """
        Returns the set of lanes currently occupied
        :param lanes: tuple of lanes, with ``min`` and ``max`` y coordinates
        :return: busy lanes set
        """
        busy_lanes = set()
        y = self._position[1]
        half_w = self._width // 2
        for lane_idx, lane in enumerate(lanes):
            if lane['min'] <= y - half_w <= lane['max'] or lane['min'] <= y + half_w <= lane['max']:
                busy_lanes.add(lane_idx)
        return busy_lanes

    @property
    def safe_distance(self):
        factor = random.gauss(1, .03)  # 0.9 Germany, 2 safe
        return self._speed * factor

    @property
    def front(self):
        return int(self._position[0] + self._length)

    @property
    def back(self):
        return int(self._position[0])

    def _brake(self, fraction):
        if self._passing: return 0
        # Maximum braking acceleration, eq. (1) from
        # http://www.tandfonline.com/doi/pdf/10.1080/16484142.2007.9638118
        g, mu = 9.81, 0.9  # gravity and friction coefficient
        acceleration = -fraction * g * mu * SCALE
        self._colour = colours['y']
        self._braked = True
        return acceleration

    def _pass(self):
        self._target_lane = self._position[1] - LANE_W
        self._passing = True
        self._colour = colours['m']
        self._braked = False

    def __gt__(self, other):
        """
        Check if self is in front of other: self.back > other.front
        """
        return self.back > other.front

    def __lt__(self, other):
        """
        Check if self is behind of other: self.front < other.back
        """
        return self.front < other.back

    def __sub__(self, other):
        """
        Return the distance between self.back and other.front
        """
        return self.back - other.front

    def policy(self, observation):
        """
        Bring together _pass, brake
        :return: acceleration, d\theta
        """
        d_direction_dt = np.zeros(2)
        d_velocity_dt = 0

        car_ahead = observation[1][1]
        if car_ahead:
            distance = car_ahead - self
            if self.safe_distance > distance > 0:
                if self._safe(observation):
                    self._pass()
                else:
                    d_velocity_dt = self._brake(min((self.safe_distance / distance) ** 0.2 - 1, 1))
            elif distance <= 0:
                self._colour = colours['r']
                self.crashed = True

        if d_velocity_dt == 0:
            d_velocity_dt = 1 * (self._target_speed - self._speed)

        if self._passing:
            error = -round(self._target_lane - self._position[1])
            d_error = error - self._error
            self._error = error
            ortho_direction = np.array((self._direction[1], -self._direction[0]))
            d_direction_dt = ortho_direction * self._speed * (3e-6 * error + 2e-3 * d_error)

        action = np.array((*d_direction_dt, d_velocity_dt))  # dx/dt, car state temporal derivative
        return action

    def _safe(self, state):
        if self.back < self.safe_distance: return False  # Cannot see in the future
        if self._passing: return False
        if not state[0]: return False  # On the leftmost lane
        if state[0][0] and self - state[0][0] < state[0][0].safe_distance: return False
        if state[0][1] and state[0][1] - self < self.safe_distance: return False
        return True


class StatefulEnv(core.Env):

    def __init__(self, display=True, nb_lanes=4, fps=30, traffic_rate=15):

        self.offset = int(1.5 * LANE_W)
        self.screen_size = (80 * LANE_W, nb_lanes * LANE_W + self.offset + LANE_W // 2)
        self.fps = fps  # updates per second
        self.delta_t = 1 / fps  # simulation timing interval
        self.nb_lanes = nb_lanes  # total number of lanes
        self.frame = 0  # frame index
        self.lanes = self.build_lanes(nb_lanes)  # create lanes object, list of dicts
        self.vehicles = None  # vehicles list
        self.traffic_rate = traffic_rate  # new cars per second
        self.lane_occupancy = None  # keeps track of what vehicle are in each lane
        self.collision = None  # an accident happened
        self.episode = 0  # episode counter
        self.car_id = None  # car counter init

        self.display = display
        if self.display:  # if display is required
            pygame.init()  # init PyGame
            self.screen = pygame.display.set_mode(self.screen_size)  # set screen size
            self.clock = pygame.time.Clock()  # set up timing

    def build_lanes(self, nb_lanes):
        return tuple(
            {'min': self.offset + n * LANE_W,
             'mid': self.offset + LANE_W / 2 + n * LANE_W,
             'max': self.offset + (n + 1) * LANE_W}
            for n in range(nb_lanes)
        )

    def reset(self):
        # Initialise environment state
        self.frame = 0
        self.vehicles = list()
        self.lane_occupancy = [[] for _ in self.lanes]
        self.episode += 1
        # keep track of car IDs so we can track the same car over several timesteps
        self.car_id = 0
        pygame.display.set_caption(f'Traffic simulator, episode {self.episode}')
        state = list()
        objects = list()
        return state, objects

    def step(self, action):

        self.collision = False
        # Free lane beginnings
        # free_lanes = set(range(self.nb_lanes))
        free_lanes = set(range(1, self.nb_lanes))

        # For every vehicle
        #   t <- t + dt
        #   leave or enter lane
        #   remove itself if out of screen
        #   update free lane beginnings
        for v in self.vehicles:
            lanes_occupied = v.get_lane_set(self.lanes)
            # Check for any passing and update lane_occupancy
            for l in range(self.nb_lanes):
                if l in lanes_occupied and v not in self.lane_occupancy[l]:
                    # Enter lane
                    bisect.insort(self.lane_occupancy[l], v)
                elif l not in lanes_occupied and v in self.lane_occupancy[l]:
                    # Leave lane
                    self.lane_occupancy[l].remove(v)
            # Remove from the environment cars outside the screen
            if v.back > self.screen_size[0]:
                for l in lanes_occupied: self.lane_occupancy[l].remove(v)
                self.vehicles.remove(v)

            # Update available lane beginnings
            if v.back < v.safe_distance:  # at most safe_distance ahead
                free_lanes -= lanes_occupied

        # Randomly add vehicles, up to 1 / dt per second
        if random.random() < self.traffic_rate * np.sin(2 * np.pi * self.frame * self.delta_t) * self.delta_t:
            if free_lanes:
                car = Car(self.lanes, free_lanes, self.delta_t, self.car_id)
                self.car_id += 1
                self.vehicles.append(car)
                for l in car.get_lane_set(self.lanes):
                    # Prepend the new car to each lane it can be found
                    self.lane_occupancy[l].insert(0, car)

        # Generate state representation for each vehicle
        vid = 0
        for v in self.vehicles:
            lane_set = v.get_lane_set(self.lanes)
            # If v is in one lane only
            # Provide a list of (up to) 6 neighbouring vehicles
            current_lane_idx = lane_set.pop()
            # Given that I'm not in the left/right-most lane
            left_vehicles = self._get_neighbours(current_lane_idx, - 1, v) \
                if current_lane_idx > 0 and len(lane_set) == 0 else (None, None)
            mid_vehicles = self._get_neighbours(current_lane_idx, 0, v)
            right_vehicles = self._get_neighbours(current_lane_idx, + 1, v) \
                if current_lane_idx < len(self.lanes) - 1 else (None, None)

            state = left_vehicles, mid_vehicles, right_vehicles

            # Compute the action
            action = v.policy(state)

            # Check for accident
            if v.crashed: self.collision = v

            v._states.append(v.get_obs(left_vehicles, mid_vehicles, right_vehicles))
            v._actions.append(torch.Tensor(action))

            #            if vid == 0:
            #                print(v._states[-1])

            # Act accordingly
            v.step(action)
            vid += 1

        # default reward if nothing happens
        reward = -0.001
        done = False

        if self.frame >= 10000:
            done = True

        if done:
            print(f'Episode ended, reward: {reward}, t={self.frame}')

        self.frame += 1

        obs = []
        # TODO: obs should be the observation of the controlled car
        return obs, reward, done, self.vehicles

    def _get_neighbours(self, current_lane_idx, d_lane, v):
        target_lane = self.lane_occupancy[current_lane_idx + d_lane]
        # Find me in the lane
        if d_lane == 0:
            my_idx = target_lane.index(v)
        else:
            my_idx = bisect.bisect(target_lane, v)
        behind = target_lane[my_idx - 1] if my_idx > 0 else None
        if d_lane == 0: my_idx += 1
        ahead = target_lane[my_idx] if my_idx < len(target_lane) else None
        return behind, ahead

    def render(self, mode='human'):
        if self.display:

            # self._pause()

            # capture the closing window and mouse-button-up event
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    sys.exit()
                elif event.type == pygame.MOUSEBUTTONUP:
                    self._pause()

            # measure time elapsed, enforce it to be >= 1/fps
            self.clock.tick(self.fps)

            # clear the screen
            self.screen.fill(colours['k'])

            # draw lanes
            for lane in self.lanes:
                sw = self.screen_size[0]  # screen width
                draw_dashed_line(self.screen, colours['w'], (0, lane['min']), (sw, lane['min']), 3)
                draw_dashed_line(self.screen, colours['w'], (0, lane['max']), (sw, lane['max']), 3)
                draw_dashed_line(self.screen, colours['r'], (0, lane['mid']), (sw, lane['mid']))

            vid = 0
            for v in self.vehicles:
                if vid == 0:
                    c = colours['r']
                else:
                    c = None
                v.draw(self.screen, c)
                vid += 1

            draw_text(self.screen, f'# cars: {len(self.vehicles)}', (10, 2))
            draw_text(self.screen, f'frame #: {self.frame}', (120, 2))

            pygame.display.flip()

            if self.collision: self._pause()

    def _pause(self):
        pause = True
        while pause:
            self.clock.tick(15)
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    sys.exit()
                elif e.type == pygame.MOUSEBUTTONUP:
                    pause = False