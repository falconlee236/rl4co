import math
from typing import Union
import torch

from tensordict.tensordict import TensorDict
from torch import Tensor

from rl4co.utils.ops import batchify
from rl4co.utils.pylogger import get_pylogger


log = get_pylogger(__name__)


def dihedral_8_augmentation(xy: Tensor) -> Tensor:
    """
    Augmentation (x8) for grid-based data (x, y) as done in POMO.
    This is a Dihedral group of order 8 (rotations and reflections)
    https://en.wikipedia.org/wiki/Examples_of_groups#dihedral_group_of_order_8

    Args:
        xy: [batch, graph, 2] tensor of x and y coordinates
    """
    # [batch, graph, 2]
    x, y = xy.split(1, dim=2)
    # augmnetations [batch, graph, 2]
    z0 = torch.cat((x, y), dim=2)
    z1 = torch.cat((1 - x, y), dim=2)
    z2 = torch.cat((x, 1 - y), dim=2)
    z3 = torch.cat((1 - x, 1 - y), dim=2)
    z4 = torch.cat((y, x), dim=2)
    z5 = torch.cat((1 - y, x), dim=2)
    z6 = torch.cat((y, 1 - x), dim=2)
    z7 = torch.cat((1 - y, 1 - x), dim=2)
    # [batch*8, graph, 2]
    aug_xy = torch.cat((z0, z1, z2, z3, z4, z5, z6, z7), dim=0)
    return aug_xy


def dihedral_8_augmentation_wrapper(
    xy: Tensor, reduce: bool = True, *args, **kw
) -> Tensor:
    """Wrapper for dihedral_8_augmentation. If reduce, only return the first 1/8 of the augmented data
    since the augmentation augments the data 8 times.
    """
    xy = xy[: xy.shape[0] // 8, ...] if reduce else xy
    return dihedral_8_augmentation(xy)


def symmetric_transform(x: Tensor, y: Tensor, phi: Tensor, offset: float = 0.5):
    """SR group transform with rotation and reflection
    Like the one in SymNCO, but a vectorized version

    Args:
        x: [batch, graph, 1] tensor of x coordinates
        y: [batch, graph, 1] tensor of y coordinates
        phi: [batch, 1] tensor of random rotation angles
        offset: offset for x and y coordinates
    """
    x, y = x - offset, y - offset
    # random rotation
    x_prime = torch.cos(phi) * x - torch.sin(phi) * y
    y_prime = torch.sin(phi) * x + torch.cos(phi) * y
    # make random reflection if phi > 2*pi (i.e. 50% of the time)
    mask = phi > 2 * math.pi
    # vectorized random reflection: swap axes x and y if mask
    xy = torch.cat((x_prime, y_prime), dim=-1)
    xy = torch.where(mask, xy.flip(-1), xy)
    return xy + offset


def symmetric_augmentation(xy: Tensor, num_augment: int = 8, first_augment: bool = False):
    """Augment xy data by `num_augment` times via symmetric rotation transform and concatenate to original data

    Args:
        xy: [batch, graph, 2] tensor of x and y coordinates
        num_augment: number of augmentations
        first_augment: whether to augment the first data point
    """
    # create random rotation angles (4*pi for reflection, 2*pi for rotation)
    phi = torch.rand(xy.shape[0], device=xy.device) * 4 * math.pi

    # set phi to 0 for first , i.e. no augmentation as in SymNCO
    if not first_augment:
        phi[: xy.shape[0] // num_augment] = 0.0
    x, y = xy[..., [0]], xy[..., [1]]
    return symmetric_transform(x, y, phi[:, None, None])


def min_max_normalize(x):
    return (x - x.min()) / (x.max() - x.min())


def get_augment_function(augment_fn: Union[str, callable]):
    if callable(augment_fn):
        return augment_fn
    if augment_fn == "dihedral8":
        return dihedral_8_augmentation_wrapper
    if augment_fn == "symmetric":
        return symmetric_augmentation
    raise ValueError(f"Unknown augment_fn: {augment_fn}")


class StateAugmentation(object):
    """Augment state by N times via symmetric rotation/reflection transform

    Args:
        env_name: environment name
        num_augment: number of augmentations
        use_dihedral_8: whether to use dihedral_8_augmentation.  If True, then num_augment must be 8
        normalize: whether to normalize the augmented data
        feats: list of features to augment
    """

    def __init__(
        self,
        num_augment: int = 8,
        augment_fn: Union[str, callable] = 'symmetric', 
        first_aug_identity: bool = True,
        normalize: bool = False,
        feats: list = None,
    ):
        self.augmentation = get_augment_function(augment_fn)
        assert not (
            self.augmentation == dihedral_8_augmentation_wrapper and num_augment != 8
        ), "If using the `dihedral8` augmentation function, then num_augment must be 8"

        if feats is None:
            log.info("Default augment feature: `locs`")
        self.feats = ["locs"] if feats is None else feats
        self.num_augment = num_augment
        self.normalize = normalize
        self.first_aug_identity = first_aug_identity

    def __call__(self, td: TensorDict) -> TensorDict:
        td_aug = batchify(td, self.num_augment)
        for feat in self.feats:
            init_aug_feat = td_aug[feat][*td.size(), 0].clone()
            aug_feat = self.augmentation(td_aug[feat], self.num_augment)
            if self.normalize:
                aug_feat = min_max_normalize(aug_feat)
            if self.first_aug_identity:
                aug_feat[*td.size(), 0] = init_aug_feat
            td_aug[feat] = aug_feat

        return td_aug
