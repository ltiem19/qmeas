Q: How do I setup a magnet?
Q: My magnet doesn't respond.
Q: The magnet is stuck and won't continue.
Q: The magnet will not stop at the intermediate fields I have set.
Q: What is the "verify" method used for?

A: Some devices, like superconducting magnets, require a more complex command structure.
You need both a command to set a field and another command to query the magnet
(either the current field or its status) to confirm that the magnet has reached it setpoint. 

1. Add the Magnet Device:
- Open the **Device Editor** and press **New Device**.
- Enter a name for the magnet (e.g., magnet1), set any necessary termination characters, and optionally test the connection. Save the device.

2. Create a query command for reading back the magnet's field or its status:
- Right-click on the magnet and choose Add Command….
- Select the Query checkbox
- Give this command a name (e.g., 'readfield' or 'status').
- Set the command to read a field value (e.g. FIELD?) or the magnet's status (e.g. STATE?)
- Set the right extraction method such that you will receive a pure number or a specific part of a string such as "HOLD".

3. Set Up the Command that sets the field:
- Add another write command for setting the magnet's field.
- Select the Write checkbox
- Give this command a name (e.g., setfield).
- Set the command string to set a field, e.g. FIELD [%], where [%] represents the value that is to be set
- Select method "verify"
- Now it depends on what your previous reading command does:
  3.1. you read the actual field:
     enter [%] in the field for "...equals"
     and add a reasonable tolerance (do not use 0!), e.g. 0.01.
     In this example, your target is "[%] +/- 0.01" 
  3.2. you read a status:
     enter whatever string the status read gives you when the magnet is at the target.
     Typical examples are "HOLD", "HOLDING", "STOP"
     
-----------------------------

Q: How do I make a nested measurement?
Q: Explain nested tasks.
Q: What does nesting mean?

A: You do nested measurements when you want to study a device as a function of 2 to 4 parameters at the same time.

For example if you want to measure the sample resistance as a function of magnetic field and a gate voltage
you first define the individual measurments for the magnet and gatevoltage.

magnet_setfield from 0 to 10 in 100 steps with 1 sec integration time
and directly underneath 
gatevoltage_set from -1 to 1 in 100 steps with 1 sec integration time

As of now, these are two separate measurements, one for the magnet and one for the gatevoltage.

Now do a right-mouse click on the row number next to gatevoltage_set an select NEST, which will indent gatevoltage_set

This generates two nested loops:
The gatevoltage will sweep from -1 to 1 at  0.0T
The gatevoltage will sweep from -1 to 1 at  0.1T
The gatevoltage will sweep from -1 to 1 at  0.2T
...
The gatevoltage will sweep from -1 to 1 at 10.0T

The data will be saved in one single file.

You can also use two voltages or two voltages and a frequency. Note that when you want to use 
a magnet, you ALWAYS need to make sure it uses a verification of the actual field.
Ask "How do I setup a magnet?" in this chat to learn how this is done.

-----------------------------

Q: I saw the method "ramp" while defining a command. What does it mean?
Q: What is ramping?
Q: Is ramping used for magnets?
Q: I have sensitive samples and want to make sure that voltages do not change too rapidly.
Q: How do I set my device to ramp?


A: 
IMPORTANT: ramping is NOT for magnets. A magnet needs time to reach its field, so it uses
the "verify" method (see "How do I setup a magnet?"). Ramping is for devices that reach a
value INSTANTLY but that you want to move gently, like a voltage source on a sensitive sample.

Some important distinctions:

1. Ramping is a qmeas-controlled ramp. It doesn't happen in your device.
Some devices have the option to perform automatic ramps. That is different.
2. Ramping is used for devices that IMMEDIATELY reach their setpoint like a voltage source,
but NOT for devices that need time to reach a setpoint like superconducting magnets.
For the latter, you need the method "verify". 
3. Ramping requires that you define a secondary command that reads the current output.
This ensure that you always ramp to the starting point, regardless of the current value.


1. Add a new device:
- Open the **Device Editor** and press **New Device**.
- Enter a name (e.g., source), set any necessary termination characters, and optionally test the connection. Save the device.

2. Create a query command for reading back the devices's output:
- Right-click on "source" and choose Add Command….
- Select the Query checkbox
- Give this command a name (e.g., 'read').
- Set the command to read the current ouput (e.g. SOURCE:VOLT?)
- Set the right extraction method such that you will receive a pure number

3. Set Up the Command that sets the output value:
- Add another write command for setting output values.
- Select the Write checkbox
- Give this command a name (e.g., setvoltage).
- Set the command string to set a field, e.g. SOURCE:VOLT [%], where [%] represents the value that is to be set
- Select method "ramp"
- Select the command that read the output (e.g. 'read' as the previous example)
- define a ramp rate per second

 
-----------------------------

Q: I cannot connect to my device!
Q: My device doesn't show after scanning.
Q: I cannot get a connection to my device.

A: Not all devices that are connected will show on the list of available devices.
TCPIP devices might now show but will still be available when you manually enter their 
address and assign it to the device name. 

To check if a TCPIP is live, open a shell (cmd) and enter ping <IP>. If It doesn't respond,
the device/network has a problem. Check the IP, restart it and try again.

GPIB is typically bulletproof. Issues might be a bad GPIB cable (yes, that is a real thing),
or same GPIB addresses to different devices. If you use a USB-GPIB adapter, you typically
need to install NI software for it to run properly.

USB devices may also require a third-party driver.


-----------------------------

Q: How do I link devices?
Q: Was does linking mean?

A: Linking devices means that a mother devices dictates the behavior of its children.
For example have generated a task 

source1_setvoltage from 0 to 1 in 10 steps with 1s integration
Now you add source2_setvoltage directly underneath.
Highlight both rows, right-click the row number and select "LINK"; a black box appears around 
both entries. Now enter an equation that contains [%] such as 2*[%]-0.1. [%] represents the
current value source1_setvoltage. For example: 
source1setvoltage = 0.0V ==> source2_setvoltage will be 2*0-0.1 = -0.1V
source1setvoltage = 0.1V ==> source2_setvoltage will be 2*0.1-0.1 = 0.1V
...
source1setvoltage = 1.0V ==> source2_setvoltage will be 2*1-0.1 = 1.9V

You can link up to 4 devices to the mother, but you cannot interlink the second device 
to the third directly.

-----------------------------

Q: I want pause a measurement until the temperature as dropped below a threshold.
Q: Can I add an if command?
Q: Can I make conditional measurements?

A:
You can define a conditional command by right-clicking on "control" and select "Add virtual while".

Give your conditional command a name such as "waituntil4K" and select an exit loop. The exit loop needs to be an 
existing query/read. For example, select a command that reads the temperature of a cryostat called "cryo_temperature".
Then select the operator "<" and enter the value 4.2. Add a time-out for when leaving the loop.

When you put "waituntil4K" into the task list, it will check the temperature every x seconds as defined by the field
"integration time" until the temperature has dropped below 4.2 Kelvin before proceeding. If you had set a (short)
timeout other than 0, qmeas might exit BEFORE 4.2K are reached.

