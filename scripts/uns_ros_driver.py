#!/usr/bin/python

"""
A ROS wrapper for the UNS driver, based on example code provided by Nortek.
The UNS driver is inherited into a new class where it is modified in order to better organize how the
extracted packages from the UNS are handled.
"""

import rospy
from std_msgs.msg import Float32
from sensor_msgs.msg import Imu
from geometry_msgs.msg import PoseWithCovarianceStamped, TwistWithCovarianceStamped
from UnsDriverTwo import UnsDriverThread
from tf.transformations import quaternion_from_euler
import socket
import time
import serial

class NORTEK_DEFINES:
    """
    This class just serves as a pseudo-namespace for constants defined by nortek
    """
    IMU_DATA_ID = "0x82"
    MAG_DATA_ID = "0x87"
    GNSS_DATA_ID = "0x91"
    ALT_DATA_ID = "0xaa"
    DVL_DATA_ID = "0xb4"
    WATER_TRACK_DATA_ID = "0xbe"
    AHRS_DATA_ID = "0xd2" 
    INS_DATA_ID = "0xdc"

    INVALID_FOM = 9.9   # Data sheet says 10.0, set lower here to be on the safe side :)
    INVALID_DISTANCE = 0.0
    INVALID_VELOCITY = -30.0 # Data sheet says -32.768, set higher here to be on the safe side :)

    # See page 15 of the communication interface spec
    AHRS_CALIBRATING = 0
    AHRS_INITIALIZING = 1
    AHRS_REGULAR_MODE = 2

class UnsRosDriver(UnsDriverThread):

    def __init__(self):
        # TODO: Get as rosparam - could maybe get port automatically from device ID
        port = '/dev/ttyUSB0'
        baud = 115200
        self.use_queues = False
        self.uns_frame_id = "uns_link"

        self.hostname = "129.241.187.19"
        self.port = 9000

        try:
            rospy.loginfo("Getting host by name...")
            self.ip = socket.gethostbyname(self.hostname)
        except socket.gaierror as e:
            rospy.loginfo("Failed to get host by name, exiting")
            exit()

        UnsDriverThread.__init__(self,
            connection_type="tcp",
            tcp_ip=self.ip,
            tcp_hostname=self.hostname,
            tcp_port=self.port,
            use_queues=self.use_queues,
            timeout=3
        )
        #UnsDriverThread.__init__(self, connection_type='serial', serial_port=port, serial_baudrate=baud, use_queues=False)

        # if self.tcp_ip is None and self.tcp_hostname is not None:
        #     self.set_tcp_ip()

        self.imu_data_pub = rospy.Publisher("/uns/imu_data", Imu, queue_size=10)
        self.imu_pub_seq = 0

        self.dvl_twist_pub = rospy.Publisher("/uns/dvl_data", TwistWithCovarianceStamped, queue_size=10)
        self.dvl_pub_seq = 0

        self.ahrs_pose_pub = rospy.Publisher("/uns/ahrs_pose", PoseWithCovarianceStamped, queue_size=10)
        self.ahrs_pub_seq = 0

        self.altitude_pub = rospy.Publisher("/uns/altitude", Float32, queue_size=10)

        connected = self.connect_uns()
        if not connected:
            print('failed to connect to the UNS. exiting...')
            exit()

        #rospy.loginfo("Starting thread")
        #self.start() # Thread start
        rospy.loginfo("Starting UNS")
        self.start_uns()
        rospy.loginfo("Running UNS")
        self.run() #blocking
    
    def __del__(self):
        self.stop_uns()
        self.stop()
        self.join(timeout=10)
        rospy.loginfo("Stopping UNS...")

    def write_packet(self, packet):
        """
        This function is executed whenever a package with sensor data is extracted from the UNS data stream. Overwriting
        this function in this class allow a user to handle these packages as they see fit.
        """
        id = packet['id']

        if id == NORTEK_DEFINES.IMU_DATA_ID:
            imu_msg = Imu()

            status = packet['status']

            imu_msg.header.seq = self.imu_pub_seq
            imu_msg.header.stamp = rospy.Time.now()
            imu_msg.header.frame_id = self.uns_frame_id

            imu_msg.linear_acceleration.x = packet['accelerometer_x']
            imu_msg.linear_acceleration.y = packet['accelerometer_y']
            imu_msg.linear_acceleration.z = packet['accelerometer_z']
            imu_msg.linear_acceleration_covariance = [0, 0, 0,
                                                      0, 0, 0,
                                                      0, 0, 0] # Row major about x, y, z

            imu_msg.angular_velocity.x = packet['gyro_x']
            imu_msg.angular_velocity.y = packet['gyro_y']
            imu_msg.angular_velocity.z = packet['gyro_z']
            imu_msg.angular_velocity_covariance = [0, 0, 0,
                                                   0, 0, 0,
                                                   0, 0, 0] # Row major about x, y, z

            self.imu_data_pub.publish(imu_msg)
            self.imu_pub_seq += 1

        elif id == NORTEK_DEFINES.MAG_DATA_ID:

            magnetometer_x = packet['magnetometer_x']
            magnetometer_y = packet['magnetometer_y']
            magnetometer_z = packet['magnetometer_z']

        elif id == NORTEK_DEFINES.DVL_DATA_ID:
            # Data from the three angled transducers
            # This could be simplified using the status bit, see page 11 of the communication spec,
            # but is left like this since we explicitly retrieve the data

            #status = package['status']
                    
            v_b   = [packet['velocity_beam_0'], packet['velocity_beam_1'], packet['velocity_beam_2']]
            d_b   = [packet['distance_beam_0'], packet['distance_beam_1'], packet['distance_beam_2']]
            fom_b = [packet['fom_beam_0'], packet['fom_beam_1'], packet['fom_beam_2']]
            fom_xyz = [packet['fom_x'], packet['fom_y'], packet['fom_z']]

            #any_invalid_data = (~status & 0xFFFFFFFF) & 0x3FFFF # First & is to limit to 32 bit precision and second is to cut off the 18 bits we use
            # Alternatively: any_invalid_data = status < 0x3FFFF
            # Check validity of incoming data, note that we avoid == to account for precision errors
            invalid_data = ""
            if any(velocity <= NORTEK_DEFINES.INVALID_VELOCITY for velocity in v_b):
                invalid_data += "beam-velocity "

            if any(distance <= NORTEK_DEFINES.INVALID_DISTANCE for distance in d_b):
                invalid_data += "beam-distance "
            
            if any(fom >= NORTEK_DEFINES.INVALID_FOM for fom in fom_b):
                invalid_data += "beam-fom "
            
            if any(fom >= NORTEK_DEFINES.INVALID_FOM for fom in fom_xyz):
                invalid_data += "xyz-fom "

            if invalid_data != "":
                rospy.logwarn("Invalid { %s} received. Ignoring package..." % invalid_data)
                return
            
            #pressure = package['pressure']

            # TODO: Make TwistStamped message out of velocities (not Odom like the dvl1000, because we get depth from ahrs here)
            dvl_msg = TwistWithCovarianceStamped()

            dvl_msg.header.seq = self.dvl_pub_seq
            dvl_msg.header.stamp = rospy.Time.now()
            dvl_msg.header.frame_id = self.uns_frame_id

            dvl_msg.twist.twist.linear.x = packet['velocity_x']
            dvl_msg.twist.twist.linear.y = packet['velocity_y']
            dvl_msg.twist.twist.linear.z = packet['velocity_z']

            var_x = fom_xyz[0] * fom_xyz[0]
            var_y = fom_xyz[1] * fom_xyz[1]
            var_z = fom_xyz[2] * fom_xyz[2]

            dvl_msg.twist.covariance = [var_x,  0,    0,   0, 0, 0,
                                          0,  var_y,  0,   0, 0, 0,
                                          0,    0,  var_z, 0, 0, 0,
                                          0,    0,    0,   0, 0, 0,
                                          0,    0,    0,   0, 0, 0,
                                          0,    0,    0,   0, 0, 0]

            self.dvl_twist_pub.publish(dvl_msg)
            self.dvl_pub_seq += 1


        elif id == NORTEK_DEFINES.AHRS_DATA_ID:

            status = packet['status']
            op_mode = packet['operation_mode'] # == status?

            # Currently always in calibrating mode? TODO: Ask :)
            #if op_mode == AHRS_CALIBRATING:
            #    rospy.logwarn("AHRS calibrating...")
            #    return
            #
            #if op_mode == AHRS_INITIALIZING:
            #    rospy.logwarn("AHRS initializing...")
            #    return
            
            fom_ahrs = packet['fom_ahrs']
            fom_field_calib = packet['fom_fc1']

            ahrs_pose = PoseWithCovarianceStamped()

            ahrs_pose.header.seq = self.ahrs_pub_seq
            ahrs_pose.header.stamp = rospy.Time.now()
            ahrs_pose.header.frame_id = self.uns_frame_id
            
            ahrs_pose.pose.pose.position.z = packet['depth']
            
            ahrs_pose.pose.pose.orientation.x = packet['quaternion_1']
            ahrs_pose.pose.pose.orientation.y = packet['quaternion_2']
            ahrs_pose.pose.pose.orientation.z = packet['quaternion_3']
            ahrs_pose.pose.pose.orientation.w = packet['quaternion_0']

            var_z = 0
            var_rx = 0
            var_ry = 0
            var_rz = 0
            ahrs_pose.pose.covariance = [0, 0,   0,     0,     0,     0,
                                         0, 0,   0,     0,     0,     0,
                                         0, 0, var_z,   0,     0,     0,
                                         0, 0,   0,  var_rx,   0,     0,
                                         0, 0,   0,     0,  var_ry,   0,
                                         0, 0,   0,     0,     0,  var_rz]

            self.ahrs_pose_pub.publish(ahrs_pose)
            self.ahrs_pub_seq += 1

        elif id == NORTEK_DEFINES.ALT_DATA_ID:

            quality = packet['altimeter_quality']

            distance = Float32()
            distance.data = packet['altimeter_distance']

            self.altitude_pub.publish(distance)

        

    # def write_condition(self, error_message, package):
    #     """
    #     This function is executed whenever an error occurs either in the UNS or with the parsing of its data. Overwriting
    #     this function in this class allow a user to handle these packages as they see fit.
    #     """
    #     rospy.logerr("UNS error: %s" % error_message)

    # def write_ascii(self, ascii_packet):
    #     """
    #     This function is executed whenever an ascii string is sent from the UNS. Overwriting his function in this class
    #     allow a user to handle these packages as they see fit.
    #     """
    #     ascii_string = "".join([chr(v) for v in ascii_packet])
    #     rospy.loginfo("ASCII string received from UNS: %s" % ascii_string)


if __name__ == "__main__":

    rospy.init_node("uns_driver", anonymous=False)

    uns_ros_driver = UnsRosDriver()

    while uns_ros_driver.driver_running and not rospy.is_shutdown():
        # This just loops while the driver is running in a separate thread
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            break

    uns_ros_driver.driver_running = False
