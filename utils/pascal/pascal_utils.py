import glob
import logging
import os
import xml.etree.ElementTree as ET

import cv2
import pandas as pd
import torch
from sklearn.preprocessing import LabelEncoder
from torch.utils.data import Dataset
from torchvision import transforms
import albumentations as A

logger = logging.getLogger(__name__)
encoder = LabelEncoder()


def convert_annotations_to_df(annotation_dir, image_dir, image_set="train"):
    xml_list = []
    for xml_file in glob.glob(annotation_dir + "/*.xml"):
        tree = ET.parse(xml_file)
        root = tree.getroot()
        for member in root.findall("object"):
            bbx = member.find("bndbox")
            xmin = int(bbx.find("xmin").text)
            ymin = int(bbx.find("ymin").text)
            xmax = int(bbx.find("xmax").text)
            ymax = int(bbx.find("ymax").text)
            label = member.find("name").text

            value = (
                root.find("filename").text,
                int(root.find("size")[0].text),
                int(root.find("size")[1].text),
                label,
                xmin,
                ymin,
                xmax,
                ymax,
            )
            xml_list.append(value)

    column_name = [
        "filename",
        "width",
        "height",
        "class",
        "xmin",
        "ymin",
        "xmax",
        "ymax",
    ]

    xml_df = pd.DataFrame(xml_list, columns=column_name)
    xml_df["filename"] = [
        os.path.join(image_dir, xml_df["filename"][i]) for i in range(len(xml_df))
    ]

    if image_set == "train":
        # label encoder encodes the labels from 0
        # we need to add +1 so that labels are encode from 1 as our
        # model reserves 0 for background class.
        xml_df["labels"] = encoder.fit_transform(xml_df["class"]) + 1
    elif image_set == "val" or image_set == "test":
        xml_df["labels"] = encoder.transform(xml_df["class"]) + 1
    return xml_df

def clamp_bbox_coordinates(bboxes):
    clamped_bboxes = []
    for bbox in bboxes:
        if len(bbox) == 4: 
            x_min, y_min, x_max, y_max = bbox
            clamped_bboxes.append((
                max(0.0, min(x_min, 1.0)),
                max(0.0, min(y_min, 1.0)),
                max(0.0, min(x_max, 1.0)),
                max(0.0, min(y_max, 1.0))
            ))
        else: 
            x_min, y_min, x_max, y_max, class_id = bbox
            clamped_bboxes.append((
                max(0.0, min(x_min, 1.0)),
                max(0.0, min(y_min, 1.0)),
                max(0.0, min(x_max, 1.0)),
                max(0.0, min(y_max, 1.0)),
                class_id
            ))
    return clamped_bboxes


class PascalDataset(Dataset):
    """
    Creates a object detection Dataset instance.

    The dataset `__getitem__` should return:
      - image: a Tensor of size `(channels, H, W)`
      - target: a dict containing the following fields
        * `boxes (FloatTensor[N, 4])`: the coordinates of the N bounding boxes in `[x0, y0, x1, y1]` format, 
                                       ranging from 0 to W and 0 to H
        * `labels (Int64Tensor[N])`: the label for each bounding box. 0 represents always the background class.
        * `image_id (Int64Tensor[1])`: an image identifier. It should be unique between all the images in the dataset, 
                                       and is used during evaluation
        * `area (Tensor[N])`: The area of the bounding box. This is used during evaluation with the COCO metric, 
                              to separate the metric scores between small, medium and large boxes.
        * `iscrowd (UInt8Tensor[N])`: instances with iscrowd=True will be ignored during evaluation.
      - image_id (Int64Tensor[1]): an image identifier. It should be unique between all the images in the dataset, 
                                   and is used during evaluation.
    Args:
        1. dataframe : A pd.Dataframe instance or str corresponding to the 
                       path to the dataframe.
        For the Dataframe the `filename` column should correspond to the path to the images.
        Each row to should one annotations in the the form `xmin`, `ymin`, `xmax`, `yman`.
        Labels should be integers in the `labels` column.
        To convert the pascal voc data in csv format use the `get_pascal` function.
        
        2. transforms: (A.Compose) transforms should be a albumentation transformations.
                        the bbox params should be set to `pascal_voc` & to pass in class
                        use `class_labels`                  
    """

    def __init__(self, dataframe, transforms):
        if isinstance(dataframe, str):
            dataframe = pd.read_csv(dataframe)

        self.tfms = transforms
        self.df = dataframe
        self.image_ids = self.df["filename"].unique()

    def __len__(self) -> int:
        return len(self.image_ids)

    def __getitem__(self, index: int):
        # Grab the Image
        image_id = self.image_ids[index]
        im = cv2.cvtColor(cv2.imread(image_id), cv2.COLOR_BGR2RGB)

        # extract the bounding boxes
        records = self.df[self.df["filename"] == image_id]
        boxes = records[["xmin", "ymin", "xmax", "ymax"]].values

        # claculate area
        area = (boxes[:, 3] - boxes[:, 1]) * (boxes[:, 2] - boxes[:, 0])
        area = torch.as_tensor(area, dtype=torch.float32)

        # Grab the Class Labels
        class_labels = records["labels"].values.tolist()

        # suppose all instances are not crowd
        iscrowd = torch.zeros((records.shape[0],), dtype=torch.int64)

        # Function to check if bbox is within 0 and 1
        def is_bbox_valid(bbox):
            x_min, y_min, x_max, y_max = bbox
            return 0 <= x_min <= 1 and 0 <= y_min <= 1 and 0 <= x_max <= 1 and 0 <= y_max <= 1

        # Filter bboxes that are within the bounds [0, 1]
        f_boxes = [bbox for bbox in boxes if is_bbox_valid(bbox)]     

        # apply transformations
        tran = self.tfms(image=im, bboxes=f_boxes, class_labels=class_labels)
        image = tran["image"]
        transformed_boxes = tran["bboxes"]
        class_labels = tran["class_labels"]
        

        
        # Filter bboxes that are within the bounds [0, 1]
        filtered_bboxes = [bbox for bbox in transformed_boxes if is_bbox_valid(bbox)]
        
        # If any bbox was outside [0, 1], discard this sample
        if len(filtered_bboxes) != len(transformed_boxes):
            # Return None or a special value to indicate that this sample should be skipped
            return None
        
        # Otherwise, convert the filtered bboxes and class_labels to tensors
        boxes = torch.tensor(filtered_bboxes, dtype=torch.float32)
        class_labels = torch.tensor(class_labels, dtype=torch.int64)

        
        # adjust boundaries
        boxes = clamp_bbox_coordinates(boxes)
        
        # target dictionary
        target = {}
        image_idx = torch.tensor([index])
        target["image_id"] = image_idx
        target["boxes"] = boxes
        target["labels"] = class_labels
        target["area"] = area
        target["iscrowd"] = iscrowd
        return image, target, image_idx


def get_pascal(annot_dir, image_dir, image_set="train", **kwargs):
    n = f"pascal_{image_set}.csv"
    df = convert_annotations_to_df(annot_dir, image_dir, image_set)
    df.to_csv(n, index=False)
    logger.info(f"DataFrame generated is saved to {n}")
    ds = PascalDataset(df, **kwargs)
    return ds

