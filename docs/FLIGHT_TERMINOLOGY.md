# Flight Time Terminology Explanation

## Understanding Flight Times

When viewing flight information, you'll see different types of times. Here's what each means:

### **Scheduled** (Original Plan)
- **What it is**: The original planned time from the airline's schedule
- **When it's set**: Before the flight, when the schedule is published
- **Example**: "Scheduled: 8:26 AM" means the airline originally planned for the flight to land at 8:26 AM
- **Color**: Gray/Muted text

### **Estimated (EST)** (Predicted Time)
- **What it is**: The current predicted time based on real-time flight tracking
- **When it's shown**: Before the event happens (flight hasn't landed/departed yet)
- **Example**: "Estimated: 8:43 AM" means based on current flight progress, it's predicted to land at 8:43 AM
- **Color**: Blue/Primary text (for runway) or Green (for gate)
- **Note**: This updates as the flight progresses and conditions change

### **Actual** (What Actually Happened)
- **What it is**: The real time when the event actually occurred
- **When it's shown**: After the event happens (flight has already landed/departed)
- **Example**: "Actual: 8:43 AM" means the flight actually landed at 8:43 AM
- **Color**: Green/Success text
- **Note**: Once actual time is available, it replaces the estimated time

## Two Types of Arrival Times

### **Runway Landing** (Touchdown)
- When the plane's wheels touch the runway
- This is the first contact with the ground
- Example: "Runway Landing: Scheduled 8:26 AM, Estimated 8:43 AM"

### **Gate Arrival** (At Gate)
- When the plane reaches the gate and passengers can disembark
- This happens after landing, taxiing, and parking
- Usually 5-15 minutes after runway landing
- Example: "Gate Arrival: Scheduled 8:41 AM, Estimated 8:50 AM"

## Example Scenario

For flight NK 1472:
- **Runway Landing**:
  - Scheduled: 8:26 AM (original plan)
  - Estimated/Actual: 8:43 AM (what happened - 17 minutes late)

- **Gate Arrival**:
  - Scheduled: 8:41 AM (original plan)
  - Estimated/Actual: 8:50 AM (what happened - 9 minutes late)

The difference between runway (8:43 AM) and gate (8:50 AM) is about 7 minutes, which is the time it takes to taxi from the runway to the gate.

## Why Times Might Differ from FlightAware

- **Data Source**: We use AeroAPI, which may update at slightly different intervals than FlightAware
- **Time Zones**: All times are converted to Eastern Time (EST/EDT)
- **Refresh Timing**: Times update when you click "Refresh" - FlightAware may have more recent data if you haven't refreshed recently

## Best Practice

1. **Before the flight**: Look at "Estimated" times - these are predictions
2. **After the flight**: Look for "Actual" times (if shown) - these are what happened
3. **Always check**: Gate arrival time is most important for pickup timing
4. **Refresh regularly**: Click "Refresh All Flights" to get the latest data

