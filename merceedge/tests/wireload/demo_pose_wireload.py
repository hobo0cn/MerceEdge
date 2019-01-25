
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2' 
import cv2
import json
import time
import numpy as np
import tensorflow as tf
from queue import Queue
from threading import Thread

from merceedge.tests.detect_object.utils.app_utils import FPS, WebcamVideoStream, draw_boxes_and_labels
from merceedge.tests.detect_object.object_detection.utils import label_map_util
from merceedge.core import WireLoad

CWD_PATH = os.path.dirname(os.path.realpath(__file__))
# CWD_PATH = os.getcwd()

# Path to frozen detection graph. This is the actual model that is used for the object detection.
MODEL_NAME = 'ssd_mobilenet_v1_coco_11_06_2017'
PATH_TO_CKPT = os.path.join(CWD_PATH, '..', 'detect_object', 
                            'object_detection', MODEL_NAME, 'frozen_inference_graph.pb')

# List of the strings that is used to add correct label for each box.
PATH_TO_LABELS = os.path.join(CWD_PATH, '..', 'detect_object', 
                            'object_detection', 'data', 'mscoco_label_map.pbtxt')

NUM_CLASSES = 90

# Loading label map
label_map = label_map_util.load_labelmap(PATH_TO_LABELS)
categories = label_map_util.convert_label_map_to_categories(label_map, max_num_classes=NUM_CLASSES,
                                                            use_display_name=True)
category_index = label_map_util.create_category_index(categories)


def detect_objects(image_np, sess, detection_graph):
    # Expand dimensions since the model expects images to have shape: [1, None, None, 3]
    image_np_expanded = np.expand_dims(image_np, axis=0)
    image_tensor = detection_graph.get_tensor_by_name('image_tensor:0')

    # Each box represents a part of the image where a particular object was detected.
    boxes = detection_graph.get_tensor_by_name('detection_boxes:0')

    # Each score represent how level of confidence for each of the objects.
    # Score is shown on the result image, together with the class label.
    scores = detection_graph.get_tensor_by_name('detection_scores:0')
    classes = detection_graph.get_tensor_by_name('detection_classes:0')
    num_detections = detection_graph.get_tensor_by_name('num_detections:0')

    # Actual detection.
    (boxes, scores, classes, num_detections) = sess.run(
        [boxes, scores, classes, num_detections],
        feed_dict={image_tensor: image_np_expanded})

    # Visualization of the results of a detection.
    rect_points, class_names, class_colors = draw_boxes_and_labels(
        boxes=np.squeeze(boxes),
        classes=np.squeeze(classes).astype(np.int32),
        scores=np.squeeze(scores),
        category_index=category_index,
        min_score_thresh=.5
    )
    return dict(rect_points=rect_points, class_names=class_names, class_colors=class_colors)


class PoseWireLoad(WireLoad):
    name = 'pose_wireload'
    
    def __init__(self, init_params={}):
        super(PoseWireLoad, self).__init__(init_params)

        self.input_q = Queue(5)  # fps is better if queue is higher but then more lags
        self.output_q = Queue()

        self.width = 960
        self.height = 544

        # setup worker thread
        # self.fps = FPS().start()

        t = Thread(target=self.worker, args=(self.input_q, self.output_q))
        t.daemon = True
        t.start()

    def process(self, frame):
        # TODO
        
        # frame = np.frombuffer(input_data, dtype=np.uint8).reshape(self.height, self.width, 3)

        # print(frame.shape)
        self.input_q.put(frame)
        
        t = time.time()

        if self.output_q.empty():
            pass  # fill up queue
        else:
            font = cv2.FONT_HERSHEY_SIMPLEX
            data = self.output_q.get()
            input_data = frame.tobytes()
            print(data)
            buf = bytes(json.dumps(data), encoding = "utf8")
            send = input_data + buf
            
            return send
            

        # self.fps.update()
        print('output empty')
        return None
        
    def worker(self, input_q, output_q):
        # Load a (frozen) Tensorflow model into memory.
        detection_graph = tf.Graph()
        with detection_graph.as_default():
            od_graph_def = tf.GraphDef()
            with tf.gfile.GFile(PATH_TO_CKPT, 'rb') as fid:
                serialized_graph = fid.read()
                od_graph_def.ParseFromString(serialized_graph)
                tf.import_graph_def(od_graph_def, name='')

            sess = tf.Session(graph=detection_graph)

        fps = FPS().start()
        while True:
            # print("worker run\n")
            fps.update()
            frame = input_q.get()
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            output_q.put(detect_objects(frame_rgb, sess, detection_graph))

        fps.stop()
        sess.close()